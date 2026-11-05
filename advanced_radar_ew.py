import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

def generate_advanced_ew_dataset(num_samples=10000):
    np.random.seed(101)
    
    print(f"Generating {num_samples} advanced synthetic EW records for Carrier Strike Group...")
    
    # 1. Feature Engineering: Realistic Tactical & Electromagnetic Parameters
    
    # Jamming Type: 0=Spot, 1=Barrage, 2=DRFM Deception, 3=Swept, 4=Cross-Polarization
    jammer_type = np.random.randint(0, 5, size=num_samples)
    
    # Signal-to-Jamming Ratio (SJR) in dB (-30 dB means heavy saturation, +5 dB means radar dominates)
    sjr_db = np.random.uniform(-30.0, 5.0, size=num_samples)
    
    # Bearing angle relative to the carrier bow (0 to 360 degrees)
    bearing_deg = np.random.uniform(0.0, 360.0, size=num_samples)
    
    # Effective Radiated Power (ERP) of the hostile emitter in Kilowatts
    erp_kw = np.random.uniform(10.0, 500.0, size=num_samples)
    
    # Atmospheric Ducting / Propagation Condition: 0 = Normal, 1 = Evaporative Duct (traps signal)
    atmospheric_ducting = np.random.randint(0, 2, size=num_samples)
    
    # Target Action Labels List
    optimal_actions = []
    
    for i in range(num_samples):
        j_type = jammer_type[i]
        sjr = sjr_db[i]
        bearing = bearing_deg[i]
        power = erp_kw[i]
        ducting = atmospheric_ducting[i]
        
        # Advanced Rule-Based Simulation Logic for Ground Truth Actions
        if sjr > -2.0 and power < 100:
            # Low threat / high radar burn-through capability
            optimal_actions.append(3) # Maintain State / Burn-Through
        elif j_type == 0 and sjr > -18.0:
            # Spot noise is narrow-band; easily mitigated by shifting frequency bands
            optimal_actions.append(0) # Change Frequency Band
        elif j_type in [1, 4] and (ducting == 1 or power > 350 or sjr < -22.0):
            # Heavy barrage or cross-pol under ducting or extreme power saturates local sensors
            optimal_actions.append(2) # Maneuver Ship (Break Line-of-Sight)
        elif j_type in [2, 3] and (60 <= bearing <= 300):
            # Deception/swept jamming off the flanks/stern allows mechanical or electronic nulling
            optimal_actions.append(1) # Rotate Radar Angle / Spatial Nulling
        else:
            # Fallback heuristic based on power density
            if power > 250:
                optimal_actions.append(2) # Maneuver Ship
            else:
                optimal_actions.append(j_type % 4)

    # Compile into a structured Pandas DataFrame
    df = pd.DataFrame({
        'jammer_type': jammer_type,
        'sjr_db': sjr_db,
        'bearing_deg': bearing_deg,
        'erp_kw': erp_kw,
        'atmospheric_ducting': atmospheric_ducting,
        'optimal_action': optimal_actions
    })
    
    return df

# ---------------------------------------------------------
# Execution Pipeline
# ---------------------------------------------------------

# Step 1: Create the dataset
dataset = generate_advanced_ew_dataset(10000)

# Step 2: Export dataset to CSV so you can view it in VS Code / Excel
csv_filename = "carrier_strike_group_ew_data.csv"
dataset.to_csv(csv_filename, index=False)
print(f"Dataset successfully saved to file: '{csv_filename}'")

# Step 3: Train an Advanced Classifier (Gradient Boosting)
X = dataset[['jammer_type', 'sjr_db', 'bearing_deg', 'erp_kw', 'atmospheric_ducting']]
y = dataset['optimal_action']

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

print("Training Advanced Gradient Boosting Decision Model...")
# Using GradientBoosting for higher predictive accuracy on complex military heuristic boundaries
model = GradientBoostingClassifier(n_estimators=150, random_state=42)
model.fit(X_train, y_train)

# Step 4: Evaluate Model Performance
y_pred = model.predict(X_test)
action_names = [
    "Change Frequency Band", 
    "Rotate Radar Angle", 
    "Maneuver Ship", 
    "Maintain State"
]

print("\n--- Model Classification Report ---")
print(classification_report(y_test, y_pred, target_names=action_names))