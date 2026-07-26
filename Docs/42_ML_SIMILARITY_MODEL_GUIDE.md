# ML Similarity Model for Merge Suggestions

## Overview

The **ML Similarity Model** is a trainable neural network that learns from user feedback to improve merge suggestion accuracy. It uses a Multi-Layer Perceptron (MLP) to predict merge confidence based on multiple features.

## Architecture

### Model Details

- **Type**: Multi-Layer Perceptron (MLPRegressor from scikit-learn)
- **Architecture**: 6 inputs → 64 neurons → 32 neurons → 1 output
- **Activation**: ReLU for hidden layers
- **Optimizer**: Adam with adaptive learning rate
- **Regularization**: L2 (alpha=0.001)
- **Early Stopping**: Enabled to prevent overfitting

### Input Features (6 features)

1. **embedding_similarity**: Cosine similarity between face embeddings (0.0-1.0)
2. **pipeline_overlap**: Ratio of common pipelines (0.0-1.0)
3. **quality_score_1**: Average embedding quality of identity 1 (0.0-1.0)
4. **quality_score_2**: Average embedding quality of identity 2 (0.0-1.0)
5. **appearances_diff**: Normalized difference in appearance counts (0.0-1.0)
6. **is_cross_pipeline**: Binary flag (0.0 or 1.0) for cross-pipeline matches

### Output

- **Predicted confidence**: Single value (0.0-1.0) indicating merge likelihood

## How It Works

### 1. Training Data Collection

The model automatically collects training data when users approve or reject merge suggestions:

- **Approved suggestions** → Positive samples (label=1.0)
- **Rejected suggestions** → Negative samples (label=0.0)

Features are extracted from:
- Identity embeddings (from FAISS)
- Pipeline information
- Quality scores
- Appearance counts

### 2. Model Training

When enough samples are collected (default: 50), the model can be trained:

```python
# Training process:
1. Load collected training samples
2. Split into train/validation sets (80/20)
3. Scale features using StandardScaler
4. Train MLPRegressor with early stopping
5. Evaluate on validation set
6. Save model to disk
```

### 3. Prediction

During merge suggestion generation:

```python
# Prediction process:
1. Extract features from identity pair
2. Scale features using saved scaler
3. Predict confidence using trained model
4. Use prediction if model is trained, otherwise use heuristic
```

## API Usage

### Train Model

**Endpoint:** `POST /api/admin/merge-suggestions/train-model`

**Parameters:**
- `min_samples` (query, optional): Minimum samples required (default: 50, min: 10, max: 1000)

**Request:**
```bash
curl -X POST "http://localhost:8000/api/admin/merge-suggestions/train-model?min_samples=50" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response (Success):**
```json
{
  "success": true,
  "message": "Model trained successfully",
  "metrics": {
    "samples_used": 75,
    "train_r2_score": 0.89,
    "val_r2_score": 0.85,
    "train_mse": 0.012,
    "val_mse": 0.018
  },
  "model_path": "models/similarity_model.pkl"
}
```

**Response (Not Enough Samples):**
```json
{
  "success": false,
  "message": "Not enough training samples: 25 < 50",
  "samples_available": 25,
  "samples_required": 50
}
```

### Get Model Status

**Endpoint:** `GET /api/admin/merge-suggestions/model-status`

**Request:**
```bash
curl "http://localhost:8000/api/admin/merge-suggestions/model-status" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response:**
```json
{
  "is_trained": true,
  "model_path": "models/similarity_model.pkl",
  "training_samples": 75,
  "model_available": true,
  "sklearn_available": true,
  "ready_to_train": true
}
```

## Configuration

Add these variables to `config.py`:

```python
# ML Similarity Model Training
SIMILARITY_MODEL_PATH: str = "models/similarity_model.pkl"
SIMILARITY_MODEL_MIN_SAMPLES: int = 50
SIMILARITY_MODEL_AUTO_TRAIN: bool = True
```

### Environment Variables

```bash
# Model path
SIMILARITY_MODEL_PATH=models/similarity_model.pkl

# Minimum samples before training
SIMILARITY_MODEL_MIN_SAMPLES=50

# Auto-train when enough samples (future feature)
SIMILARITY_MODEL_AUTO_TRAIN=true
```

## Workflow

### Initial State (No Model)

1. System uses **heuristic similarity** (weighted combination)
2. User approves/rejects suggestions
3. Training data is collected automatically
4. After 50+ samples, model can be trained

### After Training

1. System uses **trained model** for predictions
2. More accurate confidence scores
3. Better merge suggestions
4. Continues learning from new feedback

### Continuous Learning

1. New approvals/rejections add training samples
2. Retrain periodically (e.g., weekly) with more data
3. Model improves over time

## Training Metrics

### R² Score (Coefficient of Determination)
- **Range**: -∞ to 1.0
- **Interpretation**: 
  - 1.0 = Perfect predictions
  - 0.0 = Model performs as well as predicting the mean
  - < 0.0 = Model performs worse than mean
- **Good values**: > 0.7

### MSE (Mean Squared Error)
- **Range**: 0.0 to ∞
- **Interpretation**: Lower is better
- **Good values**: < 0.05

## Best Practices

### 1. Collect Enough Data
- **Minimum**: 50 samples (25 approved + 25 rejected)
- **Recommended**: 100+ samples for better accuracy
- **Ideal**: 200+ samples for production

### 2. Balanced Dataset
- Try to have similar numbers of approved and rejected samples
- If imbalanced, model may be biased

### 3. Regular Retraining
- Retrain weekly or monthly with new feedback
- More data = better model

### 4. Monitor Performance
- Check validation R² score
- If validation score drops, model may be overfitting
- Consider collecting more diverse samples

### 5. Model Versioning
- Backup model files before retraining
- Keep track of model performance over time

## Troubleshooting

### Model Not Training

**Problem**: "Not enough training samples"

**Solution**: 
- Approve/reject more merge suggestions
- Wait until you have 50+ samples
- Check status: `GET /api/admin/merge-suggestions/model-status`

### Low Accuracy

**Problem**: Low R² score (< 0.5)

**Possible Causes**:
- Not enough training data
- Imbalanced dataset (too many approved or rejected)
- Poor quality embeddings

**Solutions**:
- Collect more training samples
- Ensure balanced dataset
- Check embedding quality scores

### Model Predictions Seem Wrong

**Problem**: Model predictions don't match user expectations

**Solutions**:
- Retrain with more recent feedback
- Check if training data is representative
- Consider adjusting thresholds manually

## Technical Details

### Model Storage

- **Format**: Pickle (.pkl file)
- **Location**: `models/similarity_model.pkl` (configurable)
- **Contents**:
  - Trained MLPRegressor model
  - StandardScaler for feature normalization
  - Metadata (timestamp, training status)

### Feature Scaling

Features are normalized using `StandardScaler`:
- Mean = 0
- Standard deviation = 1
- Prevents features with large ranges from dominating

### Fallback Behavior

If model is not trained or prediction fails:
- Falls back to heuristic similarity calculation
- Uses weighted combination of embedding similarity and pipeline overlap
- Same thresholds as before model training

## Future Enhancements

Potential improvements:
- **Active Learning**: Prioritize samples that would improve model most
- **Online Learning**: Update model incrementally without full retraining
- **Ensemble Models**: Combine multiple models for better accuracy
- **Feature Engineering**: Add temporal patterns, image quality metrics
- **Deep Learning**: Upgrade to PyTorch/TensorFlow for more complex models

## Example: Complete Workflow

```python
# 1. Check model status
GET /api/admin/merge-suggestions/model-status
# Response: {"training_samples": 45, "ready_to_train": false}

# 2. Approve/reject more suggestions (collect 5+ more samples)
POST /api/admin/merge-suggestions/{id}/approve
POST /api/admin/merge-suggestions/{id}/reject

# 3. Check status again
GET /api/admin/merge-suggestions/model-status
# Response: {"training_samples": 52, "ready_to_train": true}

# 4. Train model
POST /api/admin/merge-suggestions/train-model?min_samples=50
# Response: {"success": true, "metrics": {...}}

# 5. Generate new suggestions (now using trained model)
POST /api/admin/merge-suggestions/generate-pipeline-aware
# Suggestions now use ML predictions instead of heuristics
```

## Summary

The ML Similarity Model:
- ✅ Learns from user feedback automatically
- ✅ Improves merge suggestion accuracy over time
- ✅ Uses neural network (MLP) for predictions
- ✅ Falls back to heuristics if not trained
- ✅ Easy to train via API endpoint
- ✅ Provides training metrics for monitoring

