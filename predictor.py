import joblib
import os
import math
import pandas as pd
import numpy as np
from sentence_transformers import SentenceTransformer
import torch
import torch.nn as nn
import sys

# --- CONFIGURATION & ROBUST PATHING ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR_STG1 = os.path.join(SCRIPT_DIR, "models_lgbm_tuned")
MODEL_DIR_STG2_PYTORCH = os.path.join(SCRIPT_DIR, "models_refinement_pytorch")
MAX_SEQ_LEN = 5
OUTPUT_SEQ_LEN = 3 # The decoder predicts 3 future waypoints
RNN_HIDDEN_SIZE = 128
NLP_EMBEDDING_SIZE = 384
SIGHTING_FEATURE_SIZE = 3 + NLP_EMBEDDING_SIZE # lat/lon/time + embedding
STATIC_FEATURE_SIZE = 2 # risk_level, dist_to_nearest_city

# --- PYTORCH MODEL DEFINITIONS (Restored from saved model structure) ---

class RefinementEngine(nn.Module):
    def __init__(self):
        super(RefinementEngine, self).__init__()
        # LSTM: Input size = Sighting Features (387), Hidden size = 128
        self.lstm = nn.LSTM(input_size=SIGHTING_FEATURE_SIZE, hidden_size=RNN_HIDDEN_SIZE, batch_first=True)
        
        # FC1: Input = LSTM Hidden (128) + Static Features (2) = 130
        self.fc1 = nn.Linear(RNN_HIDDEN_SIZE + STATIC_FEATURE_SIZE, 64)
        
        # FC2: Output = 2 (Lat, Lon)
        self.fc2 = nn.Linear(64, 2)

    def forward(self, seq_input, static_input):
        # 1. Process sequence with LSTM
        # We only care about the final hidden state (context)
        _, (h_n, _) = self.lstm(seq_input)
        
        # h_n shape: (num_layers, batch, hidden_size) -> (1, batch, 128)
        # Squeeze to get (batch, 128)
        context_vector = h_n[-1]
        
        # 2. Concatenate with static features
        # context_vector: (batch, 128), static_input: (batch, 2)
        combined_features = torch.cat((context_vector, static_input), dim=1)
        
        # 3. Pass through fully connected layers
        x = torch.relu(self.fc1(combined_features))
        output = self.fc2(x)
        
        return output

# --- GLOBAL MODEL LOADING ---
try:
    print("--- Sachet AI Engine Initializing: Loading all models... ---")
    
    # Load Stage 1 (LightGBM) Models
    PIPELINE_STG1 = joblib.load(os.path.join(MODEL_DIR_STG1, 'pipeline.joblib'))
    CLF_RISK = joblib.load(os.path.join(MODEL_DIR_STG1, 'clf_risk.joblib'))
    CLF_RECOVERED = joblib.load(os.path.join(MODEL_DIR_STG1, 'clf_recovered.joblib'))
    REG_TIME = joblib.load(os.path.join(MODEL_DIR_STG1, 'reg_recovery_time.joblib'))
    REG_LAT = joblib.load(os.path.join(MODEL_DIR_STG1, 'reg_recovery_lat.joblib'))
    REG_LON = joblib.load(os.path.join(MODEL_DIR_STG1, 'reg_recovery_lon.joblib'))
    print("Stage 1 (LightGBM) Models loaded successfully.")

    # Load Stage 2 (PyTorch Trajectory) Models
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    
    # Use the corrected RefinementEngine class
    REFINEMENT_MODEL = RefinementEngine().to(DEVICE)
    REFINEMENT_MODEL.load_state_dict(torch.load(os.path.join(MODEL_DIR_STG2_PYTORCH, 'refinement_model.pth'), map_location=torch.device(DEVICE)))
    REFINEMENT_MODEL.eval() 
    print("Stage 2 (PyTorch) Refinement Engine loaded successfully.")
    
    # Load NLP Model
    NLP_MODEL = SentenceTransformer('all-MiniLM-L6-v2', device=DEVICE)
    
    print("\n--- All prediction models loaded successfully. AI Engine is ready. ---")

except FileNotFoundError as e:
    # Error handling remains the same
    print("\n" + "="*80)
    print("FATAL ERROR: A required model file was not found.")
    print(f"Missing File Details: {e}")
    print("="*80 + "\n")
    sys.exit(1) 

# --- HELPER FUNCTIONS (Rest remain the same) ---
def get_feature_names_compat(enc, input_features):
    if hasattr(enc, "get_feature_names_out"): return list(enc.get_feature_names_out(input_features));
    names = [];
    for feat, cats in zip(input_features, enc.categories_):
        for c in cats: names.append(f"{feat}_{c}")
    return names

def haversine(lat1, lon1, lat2, lon2):
    """The definitive, fully vectorized haversine distance function."""
    R = 6371  # Earth radius in kilometers
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = np.radians([lat1, lon1, lat2, lon2])
    d_lon = lon2_rad - lon1_rad; d_lat = lat2_rad - lat1_rad
    a = np.sin(d_lat / 2.0)**2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(d_lon / 2.0)**2
    c = 2 * np.arcsin(np.sqrt(a)); return R * c

def prepare_input_stg1(inp: dict, pipeline: dict):
    """Prepares user input for the Stage 1 LightGBM models."""
    df = pd.DataFrame([inp])
    
    # --- Feature Engineering & Defaults ---
    
    # 1. Rename/Map basic inputs
    if 'age' in df.columns:
        df['child_age'] = df['age']
    if 'gender' in df.columns:
        df['child_gender'] = df['gender']
        
    # 2. Time features
    df['hour_sin'] = np.sin(2 * math.pi * df['abduction_time'] / 24.0)
    df['hour_cos'] = np.cos(2 * math.pi * df['abduction_time'] / 24.0)
    
    if 'missing_date' in df.columns:
        try:
            df['day_of_week'] = pd.to_datetime(df['missing_date']).dt.dayofweek
        except:
            df['day_of_week'] = 0 # Default to Monday
    else:
        df['day_of_week'] = 0
        
    # 3. Geospatial / Derived features
    CITY_CENTERS = {"Mumbai":(19.0761, 72.8775),"Pune":(18.5203, 73.8567),"Nagpur":(21.1497, 79.0806),"Nashik":(19.9975, 73.7898)}
    df['dist_to_nearest_city'] = df.apply(lambda row: min([haversine(row['latitude'], row['longitude'], c_lat, c_lon) for c_lat, c_lon in CITY_CENTERS.values()]), axis=1)
    
    # 4. Defaults for missing model features (not collected by app)
    if 'population_density' not in df.columns:
        df['population_density'] = 5000 # Default: Dense Urban-ish
    if 'transport_hub_nearby' not in df.columns:
        df['transport_hub_nearby'] = 0 # Default: No
    if 'abductor_relation' not in df.columns:
        df['abductor_relation'] = 'stranger' # Default assumption
    if 'region_type' not in df.columns:
        df['region_type'] = 'Urban' # Default assumption

    # Ensure all expected columns exist before transform
    # (The pipeline expects specific columns for scaling/encoding)
    
    # Transform
    try:
        X_num_scaled = pipeline['scaler'].transform(df[pipeline['num_cols']])
        X_cat_encoded = pipeline['encoder'].transform(df[pipeline['cat_cols']])
        
        cat_feature_names = get_feature_names_compat(pipeline['encoder'], pipeline['cat_cols'])
        
        X_final = pd.concat([
            pd.DataFrame(X_num_scaled, columns=pipeline['num_cols']),
            pd.DataFrame(X_cat_encoded, columns=cat_feature_names)
        ], axis=1)
        
        return X_final[pipeline['X_columns']]
        
    except Exception as e:
        print(f"Feature preparation error: {e}")
        # Fallback: return empty or raise
        raise e

# --- MAIN PREDICTION FUNCTIONS (Called by the App) ---
def predict_initial_case(inp: dict):
    X_in=prepare_input_stg1(inp, PIPELINE_STG1); risk_label=int(CLF_RISK.predict(X_in)[0]); risk_prob=float(CLF_RISK.predict_proba(X_in)[0].max()); rec_prob=float(CLF_RECOVERED.predict_proba(X_in)[0][1]); rec_label=1 if rec_prob>=0.5 else 0
    est_recovery_time, pred_lat, pred_lon = 0.0, 0.0, 0.0
    
    # Always predict location/time for reference, even if probability is low
    try:
        est_recovery_time = float(REG_TIME.predict(X_in)[0])
        pred_lat = float(REG_LAT.predict(X_in)[0])
        pred_lon = float(REG_LON.predict(X_in)[0])
    except Exception as e:
        print(f"Error in regression prediction: {e}")

    return {'risk_label':risk_label,'risk_prob':risk_prob,'recovered_label':rec_label,'recovered_prob':rec_prob,'recovery_time_hours':est_recovery_time,'predicted_latitude':pred_lat,'predicted_longitude':pred_lon}


def _interpolate_towards(anchor_lat, anchor_lon, target_lat, target_lon, max_distance_km):
    """Move from anchor towards target so distance from anchor stays within max_distance_km."""
    distance = float(haversine(anchor_lat, anchor_lon, target_lat, target_lon))
    if distance <= max_distance_km or distance == 0:
        return float(target_lat), float(target_lon)

    ratio = max_distance_km / distance
    clamped_lat = anchor_lat + (target_lat - anchor_lat) * ratio
    clamped_lon = anchor_lon + (target_lon - anchor_lon) * ratio
    return float(clamped_lat), float(clamped_lon)


def _blend_coordinates(base_lat, base_lon, refined_lat, refined_lon, alpha):
    """Blend refined coordinates with base coordinates using alpha in [0,1]."""
    alpha = min(1.0, max(0.0, float(alpha)))
    lat = (1.0 - alpha) * base_lat + alpha * refined_lat
    lon = (1.0 - alpha) * base_lon + alpha * refined_lon
    return float(lat), float(lon)


def _resolve_refinement_policy(sightings):
    """Return dynamic refinement parameters based on data quality/recency."""
    if not sightings:
        return {'blend_alpha': 0.0, 'max_radius_km': 45.0}

    sighting_count = len(sightings)
    latest_hours_since = min([float(s.get('hours_since', 9999)) for s in sightings]) if sightings else 9999

    # Base trust in the refinement model grows with more signals.
    blend_alpha = 0.45
    if sighting_count >= 3:
        blend_alpha = 0.58
    if sighting_count >= 5:
        blend_alpha = 0.68

    # Very fresh sightings allow slightly wider movement.
    max_radius_km = 45.0
    if latest_hours_since <= 6:
        max_radius_km = 75.0
    elif latest_hours_since <= 24:
        max_radius_km = 60.0
    elif latest_hours_since <= 72:
        max_radius_km = 50.0

    return {'blend_alpha': blend_alpha, 'max_radius_km': max_radius_km}

def refine_location_with_sightings(initial_prediction: dict, sightings: list, initial_case_input: dict):
    if not sightings or REFINEMENT_MODEL is None:
        return initial_prediction['predicted_latitude'], initial_prediction['predicted_longitude']
    
    # Static features: Risk level and Distance to nearest city
    # Calculate dist_to_nearest_city if not present
    dist_to_city = initial_case_input.get('dist_to_nearest_city')
    if dist_to_city is None:
        CITY_CENTERS = {"Mumbai":(19.0761, 72.8775),"Pune":(18.5203, 73.8567),"Nagpur":(21.1497, 79.0806),"Nashik":(19.9975, 73.7898)}
        dist_to_city = min([haversine(initial_case_input['latitude'], initial_case_input['longitude'], c_lat, c_lon) for c_lat, c_lon in CITY_CENTERS.values()])
        
    static_features=torch.tensor([[initial_prediction['risk_label'], dist_to_city]], dtype=torch.float32).to(DEVICE)
    seq_features=[]
    
    # 1. Prepare sequence features
    # hours_since is age of sighting; higher means older. Keep newest sightings in window.
    ordered_sightings = sorted(sightings, key=lambda s: s['hours_since'], reverse=True)
    for sighting in ordered_sightings:
        text_embedding=NLP_MODEL.encode(sighting['direction_text'], device=DEVICE); 
        features=[sighting['lat'],sighting['lon'],sighting['hours_since']]+list(text_embedding); 
        seq_features.append(features)
    
    # 2. Pad/Truncate the sequence (MAX_SEQ_LEN is 5)
    padded_seq=np.zeros((MAX_SEQ_LEN, SIGHTING_FEATURE_SIZE), dtype=np.float32); 
    seq_len = len(seq_features)
    
    if seq_len > 0:
        # Use the newest MAX_SEQ_LEN points while preserving temporal order within the selected window.
        seq_to_use = seq_features[-MAX_SEQ_LEN:]
        padded_seq[-len(seq_to_use):] = np.array(seq_to_use, dtype=np.float32)

    seq_tensor=torch.tensor([padded_seq], dtype=torch.float32).to(DEVICE)
    
    # 3. Run Seq2Seq Refinement
    with torch.no_grad():
        # REFINEMENT_MODEL outputs the final predicted coordinate (Batch x 2)
        refined_coords = REFINEMENT_MODEL(seq_tensor, static_features).cpu().numpy()[0]

    raw_refined_lat = float(refined_coords[0])
    raw_refined_lon = float(refined_coords[1])
    base_lat = float(initial_prediction['predicted_latitude'])
    base_lon = float(initial_prediction['predicted_longitude'])

    policy = _resolve_refinement_policy(sightings)
    blended_lat, blended_lon = _blend_coordinates(
        base_lat,
        base_lon,
        raw_refined_lat,
        raw_refined_lon,
        policy['blend_alpha']
    )

    anchor_lat = float(initial_case_input.get('latitude', base_lat))
    anchor_lon = float(initial_case_input.get('longitude', base_lon))
    final_lat, final_lon = _interpolate_towards(
        anchor_lat,
        anchor_lon,
        blended_lat,
        blended_lon,
        policy['max_radius_km']
    )

    return final_lat, final_lon
