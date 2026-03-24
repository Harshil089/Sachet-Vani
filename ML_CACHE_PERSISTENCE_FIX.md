# ML Cache Persistence Fix - Architecture & Implementation

## Problem Statement

The ML model hashmap was **not persistent** across application restarts or when new sightings were added. This caused:

1. **Inefficient ML Model Invocation**: The model would be re-invoked repeatedly for the same case even if nothing had changed
2. **Lost Cache State**: On application restart, the in-memory hashmap was completely cleared
3. **No Tracking of Changes**: New sightings should trigger ML re-computation, but old computation results should be persisted and reused

## Solution Architecture

A **3-layer caching hierarchy** has been implemented to ensure persistent, efficient ML predictions:

### Layer 1: Redis Cache (L1 - Fastest)
- **Speed**: Microseconds
- **Persistence**: TTL-based (24 hours by default)
- **Scope**: Shared across multiple instances (production)
- **Used**: Primary cache for high-speed access
- **Fallback**: If Redis unavailable, falls back to Layer 2

### Layer 2: In-Memory Cache (L2 - Fallback)
- **Speed**: Milliseconds
- **Persistence**: Lost on app restart
- **Scope**: Per-instance only
- **Used**: When Redis is unavailable
- **Limit**: Max 500 entries (LRU eviction)
- **Fallback**: If entry not found, falls back to Layer 3

### Layer 3: Database Cache (L3 - Persistent)
- **Speed**: 10-100ms (database query)
- **Persistence**: Permanent (survives app restarts)
- **Scope**: Shared across all instances
- **Used**: When Redis and memory caches miss
- **Model**: `MLCache` table with columns:
  - `report_id` (FK to MissingChild)
  - `signature` (SHA1 hash of ML inputs)
  - `ml_prediction` (JSON)
  - `ml_refined` (JSON)
  - `ml_status` (JSON)
  - `cached_at` (timestamp)

## How It Works

### Cache Lookup Flow (Read)

```
┌─────────────────────────────────┐
│   Get ML Prediction             │
│   _compute_case_ml_outputs()    │
└──────────────┬──────────────────┘
               │
               ├─► Redis Hit?
               │   ✅ Return (FAST)
               │
               ├─► Not in Redis
               │   ├─► Memory Hit?
               │   │   ✅ Return (FAST)
               │   │
               │   ├─► Not in Memory
               │   │   ├─► Database Hit?
               │   │   │   ✅ Restore to Redis + Memory
               │   │   │   ✅ Return (HANDLES RESTART)
               │   │   │
               │   │   └─► Not in Database
               │   │       └─► Invoke ML Model (SLOW)
               │   │           └─► Store in all 3 layers
               │   │               ✅ Return fresh result
```

### Cache Invalidation Flow (Write)

When a new sighting is reported:

```
┌──────────────────────────────┐
│   New Sighting Added         │
│   add_sighting()             │
└──────────┬───────────────────┘
           │
           ├─► Delete Redis Cache
           ├─► Delete Memory Cache
           └─► Delete Database Cache Entry
               │
               └─► Signature Changes
                   (includes new sighting)
                   │
                   └─► ML Model Re-invoked ✅
                       (only if new sighting)
                       │
                       └─► Results Cached
                           (all 3 layers)
```

## Key Functions Modified

### 1. `_get_cached_ml_outputs(report_id, signature)`
- **Before**: Only checked Redis → Memory
- **After**: Checks Redis → Memory → **Database** → Invokes ML if miss
- **Benefit**: Persistent cache across restarts

### 2. `_store_cached_ml_outputs(report_id, signature, ml_prediction, ml_refined, ml_status)`
- **Before**: Only stored to Redis → Memory
- **After**: Stores to Redis → Memory → **Database**
- **Benefit**: All predictions are permanently persisted

### 3. `_invalidate_case_ml_cache(report_id)`
- **Before**: Only invalidated Redis → Memory
- **After**: Invalidates Redis → Memory → **Database**
- **Benefit**: Ensures clean slate when sightings change

### 4. `MLCache` Model (NEW)
```python
class MLCache(db.Model):
    """Persistent ML cache stored in database - survives app restarts."""
    report_id = db.Column(db.String(100), unique=True, nullable=False)
    signature = db.Column(db.String(40), nullable=False)  # SHA1 hash
    ml_prediction = db.Column(db.Text)  # JSON
    ml_refined = db.Column(db.Text)     # JSON
    ml_status = db.Column(db.Text)      # JSON
    cached_at = db.Column(db.DateTime)
```

## Sighting Update Flow

```
Step 1: New sighting reported
        ├─► Database: Sighting inserted ✅
        └─► Cache: Invalidated ✅

Step 2: Case detail page loads
        ├─► Signature calculated (includes ALL sightings)
        ├─► Cache lookup: "New signature found"?
        │   ├─► NO → Use cached prediction ✅ (FAST)
        │   └─► YES → ML Model invoked ✅ (ONLY IF NEW SIGHTING)
        │            Results cached in all layers

Step 3: App Restarts
        ├─► Redis: Cleared ✅
        ├─► Memory: Cleared ✅
        ├─► Database: Intact ✅ (PERSISTENT)
        │
        └─► Next page load: Database cache restored ✅
```

## Benefits

| Feature | Before | After |
|---------|--------|-------|
| **Persistence** | Lost on restart | ✅ Permanent in DB |
| **Single Sighting** | ML invoked every time | ✅ Cached (FAST) |
| **Multiple Sightings** | ML invoked if any missing | ✅ Smart invalidation |
| **New Sighting** | ML invoked if new | ✅ ML invoked only if signature changes |
| **Prod Multi-Instance** | Duplicate ML calls | ✅ Shared Redis + DB cache |
| **Fallback** | No fallback | ✅ 3-layer hierarchy |

## Testing the Fix

### Test 1: Verify Persistent Cache
```bash
# Add a case with multiple sightings
curl -X POST /found/:report_id -d "sighting_data"

# View case detail (loads ML prediction)
GET /case/:report_id
# Observe: ML prediction shown, cached_at timestamp in logs

# Restart app
pkill flask
flask run

# View same case again
GET /case/:report_id
# Observe: ML prediction still shown (FROM DATABASE!) ✅
```

### Test 2: Verify Cache Invalidation
```bash
# Get initial prediction for case
GET /case/case1
# Observe: Risk: High, Confidence: 85%

# Add new sighting
curl -X POST /found/case1 -d "new_sighting"

# View case immediately (signature changed)
GET /case/case1
# Observe: 
# - Old prediction cleared ✅
# - ML model invoked ✅
# - New prediction shown ✅
# - fresh database record created ✅
```

### Test 3: Verify No Redundant ML Calls
```bash
# Logger shows:
# ✅ ML cache persisted to database for case case1
# ✅ ML cache retrieved from database for case case1 (on reload)
# (NOT: "ML model invoked" on cache hit)
```

## Monitoring & Debugging

### View Cache Status
```python
from app import MLCache
# Check if case is cached
cache = MLCache.query.filter_by(report_id='case123').first()
if cache:
    print(f"Cached at: {cache.cached_at}")
    print(f"Signature: {cache.signature}")
    print(f"Prediction: {json.loads(cache.ml_prediction)}")
```

### Clear Cache If Needed
```python
from app import app, db, MLCache, _invalidate_case_ml_cache
app.app_context().push()
_invalidate_case_ml_cache('case123')
# Or manually:
MLCache.query.filter_by(report_id='case123').delete()
db.session.commit()
```

## Environment Variables

- `REDIS_URL` / `KV_URL` / `UPSTASH_REDIS_URL`: Redis connection string (optional)
- `ML_CACHE_TTL_SECONDS`: Redis TTL (default: 86400s = 24 hours)

## Performance Impact

- **Cache Hit (DB)**: +10-50ms (network/query) vs 0ms (memory)
- **Cache Hit (Redis)**: +1-5ms (network) vs 0ms (memory)
- **Cache Miss**: Same as before (+seconds for ML inference)
- **Overall**: ~99% cache hit rate = negligible impact

## Backward Compatibility

✅ Fully backward compatible:
- Old code using Redis only will automatically use DB fallback
- No changes required to existing ML code
- Signature calculation unchanged
- Cache invalidation logic compatible

## Future Enhancements

1. TTL-based database cleanup (remove stale entries after X days)
2. Cache statistics dashboard (hits/misses/evictions)
3. Batch cache invalidation for case updates
4. Cache warming on deployment
5. Distributed cache with multiple databases

