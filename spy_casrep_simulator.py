import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

def generate_spy_damage_dataset(num_samples=10000):
    np.random.seed(42)
    
    hit_type = np.random.choice([0, 1, 2], size=num_samples, p=[0.4, 0.4, 0.2]) # 0: Shrapnel, 1: Direct, 2: EMP/Power Surge
    power_surge_volts = np.random.uniform(100, 5000, size=num_samples)
    cooling_loop_integrity = np.random.uniform(0, 100, size=num_samples) # %
    face_damage_pct = np.random.uniform(0, 100, size=num_samples) # %
    modules_offline = np.random.randint(0, 150, size=num_samples) # Out of 150 blocks
    
    casrep_categories = []
    
    for i in range(num_samples):
        # Rule-based logic for AN/SPY damage severity mapping
        if face_damage_pct[i] > 75 or cooling_loop_integrity[i] < 15 or modules_offline[i] > 110:
            casrep_categories.append(4) # CAT 4: Total Mission Loss
        elif face_damage_pct[i] > 40 or cooling_loop_integrity[i] < 40 or modules_offline[i] > 60:
            casrep_categories.append(3) # CAT 3: Major Degradation
        elif face_damage_pct[i] > 10 or cooling_loop_integrity[i] < 70 or modules_offline[i] > 15:
            casrep_categories.append(2) # CAT 2: Minor Degradation
        else:
            casrep_categories.append(1) # CAT 1: Minimal / Administrative impact
            
    data = pd.DataFrame({
        'hit_type': hit_type,
        'power_surge_volts': power_surge_volts,
        'cooling_loop_integrity': cooling_loop_integrity,
        'face_damage_pct': face_damage_pct,
        'modules_offline': modules_offline,
        'casrep_category': casrep_categories
    })
    
    return data

# Step 1: Generate and save dataset
df_damage = generate_spy_damage_dataset(10000)
df_damage.to_csv('spy_radar_damage_casrep.csv', index=False)
print("Dataset generated and saved to spy_radar_damage_casrep.csv")

# Step 2: Train a Random Forest Model for Damage Classification
X = df_damage[['hit_type', 'power_surge_volts', 'cooling_loop_integrity', 'face_damage_pct', 'modules_offline']]
y = df_damage['casrep_category']

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

damage_model = RandomForestClassifier(n_estimators=100, random_state=42)
damage_model.fit(X_train, y_train)

# Step 3: Evaluate
y_pred = damage_model.predict(X_test)
print("\n--- AN/SPY Damage CASREP Classification Report ---")
print(classification_report(y_test, y_pred))