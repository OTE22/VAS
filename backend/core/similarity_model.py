"""
Trainable Similarity Model for Merge Suggestions
================================================
Neural network that learns from user feedback to improve merge suggestions.
Uses scikit-learn MLPRegressor (Multi-Layer Perceptron) for similarity prediction.
"""

import os
import pickle
import logging
import numpy as np
from typing import List, Dict, Tuple, Optional
from datetime import datetime
from dataclasses import dataclass

try:
    from sklearn.neural_network import MLPRegressor
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logging.warning("scikit-learn not available. Similarity model training disabled.")

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class TrainingSample:
    """Single training sample from user feedback"""
    embedding_similarity: float  # Cosine similarity between embeddings
    pipeline_overlap: float  # Ratio of common pipelines
    quality_score_1: float  # Average quality of identity 1
    quality_score_2: float  # Average quality of identity 2
    appearances_diff: float  # Difference in appearance counts
    is_cross_pipeline: bool  # Whether identities are from different pipelines
    label: float  # Target: 1.0 if approved, 0.0 if rejected, or actual confidence
    timestamp: datetime


class SimilarityModel:
    """
    Trainable neural network for predicting merge suggestion confidence.
    
    Architecture:
    - Input: 6 features (embedding_sim, pipeline_overlap, quality1, quality2, appearances_diff, is_cross_pipeline)
    - Hidden layers: 2 layers with 64 and 32 neurons
    - Output: 1 value (predicted confidence 0.0-1.0)
    - Activation: ReLU for hidden, sigmoid for output
    """
    
    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path or settings.SIMILARITY_MODEL_PATH
        self.scaler = StandardScaler()
        self.model: Optional[MLPRegressor] = None
        self.is_trained = False
        self.training_samples: List[TrainingSample] = []
        self.last_training_metrics: Optional[Dict] = None
        
        # Create model directory if it doesn't exist
        os.makedirs(os.path.dirname(self.model_path) if os.path.dirname(self.model_path) else '.', exist_ok=True)
        
        # Try to load existing model
        self.load()
    
    def _create_model(self) -> MLPRegressor:
        """Create a new MLPRegressor model"""
        if not SKLEARN_AVAILABLE:
            raise ImportError("scikit-learn is required for similarity model training")
        
        # Multi-layer perceptron with 2 hidden layers
        # Architecture: 6 inputs -> 64 -> 32 -> 1 output
        model = MLPRegressor(
            hidden_layer_sizes=(64, 32),  # 2 hidden layers: 64 and 32 neurons
            activation='relu',  # ReLU activation
            solver='adam',  # Adam optimizer
            alpha=0.001,  # L2 regularization
            batch_size='auto',
            learning_rate='adaptive',  # Adaptive learning rate
            learning_rate_init=0.001,
            max_iter=500,  # Maximum iterations
            shuffle=True,
            random_state=42,
            tol=1e-4,  # Tolerance for convergence
            verbose=False,
            warm_start=False,
            early_stopping=True,  # Early stopping to prevent overfitting
            validation_fraction=0.1,  # 10% for validation
            n_iter_no_change=10,  # Stop if no improvement for 10 iterations
        )
        return model
    
    def load(self) -> bool:
        """Load trained model from disk"""
        if not SKLEARN_AVAILABLE:
            return False
        
        try:
            if os.path.exists(self.model_path):
                with open(self.model_path, 'rb') as f:
                    data = pickle.load(f)
                    self.model = data['model']
                    self.scaler = data['scaler']
                    self.is_trained = data.get('is_trained', False)
                    logger.info(f"[SIMILARITY_MODEL] ✅ Loaded trained model from {self.model_path}")
                return True
            else:
                logger.info(f"[SIMILARITY_MODEL] No existing model found at {self.model_path}, will train new model")
                return False
        except Exception as e:
            logger.warning(f"[SIMILARITY_MODEL] Failed to load model: {e}")
            return False
    
    def save(self) -> bool:
        """Save trained model to disk"""
        if not self.model or not self.is_trained:
            logger.warning("[SIMILARITY_MODEL] Cannot save: model not trained")
            return False
        
        try:
            data = {
                'model': self.model,
                'scaler': self.scaler,
                'is_trained': self.is_trained,
                'timestamp': datetime.utcnow().isoformat()
            }
            with open(self.model_path, 'wb') as f:
                pickle.dump(data, f)
            logger.info(f"[SIMILARITY_MODEL] ✅ Saved trained model to {self.model_path}")
            return True
        except Exception as e:
            logger.error(f"[SIMILARITY_MODEL] Failed to save model: {e}")
            return False
    
    def add_training_sample(
        self,
        embedding_similarity: float,
        pipeline_overlap: float,
        quality_score_1: float,
        quality_score_2: float,
        appearances_diff: float,
        is_cross_pipeline: bool,
        label: float,  # 1.0 for approved, 0.0 for rejected, or actual confidence
        db_session=None,  # Optional database session for persistence
        identity_id_1=None,  # Optional identity IDs for tracking
        identity_id_2=None,
        user_id=None  # Optional user ID who created this sample
    ):
        """Add a training sample from user feedback"""
        sample = TrainingSample(
            embedding_similarity=embedding_similarity,
            pipeline_overlap=pipeline_overlap,
            quality_score_1=quality_score_1,
            quality_score_2=quality_score_2,
            appearances_diff=appearances_diff,
            is_cross_pipeline=1.0 if is_cross_pipeline else 0.0,
            label=label,
            timestamp=datetime.utcnow()
        )
        self.training_samples.append(sample)
        logger.debug(f"[SIMILARITY_MODEL] Added training sample: label={label:.3f}")
        
        # Persist to database if session provided
        if db_session is not None:
            try:
                from db_models import SimilarityTrainingData
                training_data = SimilarityTrainingData(
                    identity_id_1=identity_id_1,
                    identity_id_2=identity_id_2,
                    embedding_similarity=embedding_similarity,
                    pipeline_overlap=pipeline_overlap,
                    quality_score_1=quality_score_1,
                    quality_score_2=quality_score_2,
                    appearances_diff=appearances_diff,
                    is_cross_pipeline=is_cross_pipeline,
                    label=label,
                    created_by_user_id=user_id
                )
                db_session.add(training_data)
                # Note: Don't commit here - let the caller commit
                logger.debug(f"[SIMILARITY_MODEL] Saved training sample to database")
            except Exception as e:
                logger.warning(f"[SIMILARITY_MODEL] Failed to save training sample to database: {e}")
    
    def prepare_features(self, samples: List[TrainingSample]) -> Tuple[np.ndarray, np.ndarray]:
        """Prepare features and labels from training samples"""
        X = []
        y = []
        
        for sample in samples:
            features = [
                sample.embedding_similarity,
                sample.pipeline_overlap,
                sample.quality_score_1,
                sample.quality_score_2,
                sample.appearances_diff,
                sample.is_cross_pipeline
            ]
            X.append(features)
            y.append(sample.label)
        
        X = np.array(X, dtype=np.float32)
        y = np.array(y, dtype=np.float32)
        
        return X, y
    
    def train(self, min_samples: int = 50) -> Dict:
        """
        Train the model on collected training samples.
        
        Args:
            min_samples: Minimum number of samples required for training
        
        Returns:
            Dictionary with training metrics
        """
        if not SKLEARN_AVAILABLE:
            return {
                'success': False,
                'error': 'scikit-learn not available'
            }
        
        if len(self.training_samples) < min_samples:
            return {
                'success': False,
                'error': f'Not enough training samples: {len(self.training_samples)} < {min_samples}',
                'samples': len(self.training_samples),
                'required': min_samples
            }
        
        logger.info(f"[SIMILARITY_MODEL] 🎓 Training model with {len(self.training_samples)} samples...")
        
        try:
            # Prepare features and labels
            X, y = self.prepare_features(self.training_samples)
            
            # Split into train and validation sets
            X_train, X_val, y_train, y_val = train_test_split(
                X, y, test_size=0.2, random_state=42, shuffle=True
            )
            
            # Scale features
            X_train_scaled = self.scaler.fit_transform(X_train)
            X_val_scaled = self.scaler.transform(X_val)
            
            # Create and train model
            self.model = self._create_model()
            
            logger.info(f"[SIMILARITY_MODEL] Training on {len(X_train)} samples, validating on {len(X_val)} samples...")
            self.model.fit(X_train_scaled, y_train)
            
            # Evaluate
            train_score = self.model.score(X_train_scaled, y_train)
            val_score = self.model.score(X_val_scaled, y_val)
            
            # Calculate predictions for metrics
            y_train_pred = self.model.predict(X_train_scaled)
            y_val_pred = self.model.predict(X_val_scaled)
            
            train_mse = np.mean((y_train - y_train_pred) ** 2)
            val_mse = np.mean((y_val - y_val_pred) ** 2)
            
            self.is_trained = True
            
            # Save model
            self.save()
            
            metrics = {
                'success': True,
                'samples_used': len(self.training_samples),
                'train_samples': len(X_train),
                'val_samples': len(X_val),
                'train_r2_score': float(train_score),
                'val_r2_score': float(val_score),
                'train_mse': float(train_mse),
                'val_mse': float(val_mse),
                'model_path': self.model_path
            }
            
            # Store metrics for status endpoint
            self.last_training_metrics = metrics
            
            logger.info(
                f"[SIMILARITY_MODEL] ✅ Training complete! "
                f"Train R²: {train_score:.4f}, Val R²: {val_score:.4f}, "
                f"Train MSE: {train_mse:.4f}, Val MSE: {val_mse:.4f}"
            )
            
            return metrics
            
        except Exception as e:
            logger.error(f"[SIMILARITY_MODEL] ❌ Training failed: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }
    
    def predict(
        self,
        embedding_similarity: float,
        pipeline_overlap: float,
        quality_score_1: float,
        quality_score_2: float,
        appearances_diff: float,
        is_cross_pipeline: bool
    ) -> float:
        """
        Predict merge confidence using the trained model.
        
        Args:
            embedding_similarity: Cosine similarity between embeddings
            pipeline_overlap: Ratio of common pipelines
            quality_score_1: Average quality of identity 1
            quality_score_2: Average quality of identity 2
            appearances_diff: Difference in appearance counts
            is_cross_pipeline: Whether identities are from different pipelines
        
        Returns:
            Predicted confidence score (0.0-1.0)
        """
        if not self.is_trained or not self.model:
            # Fallback to heuristic if model not trained
            return self._heuristic_prediction(
                embedding_similarity, pipeline_overlap, quality_score_1,
                quality_score_2, appearances_diff, is_cross_pipeline
            )
        
        try:
            # Prepare features
            features = np.array([[
                embedding_similarity,
                pipeline_overlap,
                quality_score_1,
                quality_score_2,
                appearances_diff,
                1.0 if is_cross_pipeline else 0.0
            ]], dtype=np.float32)
            
            # Scale features
            features_scaled = self.scaler.transform(features)
            
            # Predict
            prediction = self.model.predict(features_scaled)[0]
            
            # Clamp to [0, 1]
            prediction = max(0.0, min(1.0, prediction))
            
            return float(prediction)
            
        except Exception as e:
            logger.warning(f"[SIMILARITY_MODEL] Prediction failed: {e}, using heuristic")
            return self._heuristic_prediction(
                embedding_similarity, pipeline_overlap, quality_score_1,
                quality_score_2, appearances_diff, is_cross_pipeline
            )
    
    def _heuristic_prediction(
        self,
        embedding_similarity: float,
        pipeline_overlap: float,
        quality_score_1: float,
        quality_score_2: float,
        appearances_diff: float,
        is_cross_pipeline: bool
    ) -> float:
        """Fallback heuristic prediction when model is not trained.

        Weights come from EMBEDDING_SIMILARITY_WEIGHT / PIPELINE_SIMILARITY_WEIGHT.
        Both branches used to carry their own literal pair (0.9/0.1 and 0.7/0.3),
        which made this the fourth independent copy of the same two numbers and
        meant the declared settings governed none of them.
        """
        embedding_weight = float(settings.EMBEDDING_SIMILARITY_WEIGHT)
        pipeline_weight = float(settings.PIPELINE_SIMILARITY_WEIGHT)
        if is_cross_pipeline:
            # No shared pipeline means the overlap signal carries no
            # information, so lean further onto the embedding.
            embedding_weight = min(1.0, embedding_weight + pipeline_weight / 2.0)
            pipeline_weight = max(0.0, 1.0 - embedding_weight)
        score = (embedding_similarity * embedding_weight) + (pipeline_overlap * pipeline_weight)

        # Quality adjustment: scale the score down when the two faces are of
        # poor quality, never below the configured floor of its own value.
        quality_floor = float(settings.SIMILARITY_QUALITY_FLOOR)
        avg_quality = (quality_score_1 + quality_score_2) / 2.0
        score = score * (quality_floor + (1.0 - quality_floor) * avg_quality)

        return max(0.0, min(1.0, score))
    
    def reload_from_path(self, path: str) -> bool:
        """Atomically swap the runtime model from a verified artifact.

        Loads and smoke-tests the new model FIRST; only then swaps the
        instance attributes — a failed load leaves the current model
        serving. Used by model activation/rollback.
        """
        if not SKLEARN_AVAILABLE:
            return False
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
            new_model = data['model']
            new_scaler = data['scaler']
            # Inference smoke test before swapping anything
            import numpy as _np
            probe = _np.array([[0.9, 0.5, 0.8, 0.8, 0.1, 0.0]], dtype=_np.float32)
            prediction = float(new_model.predict(new_scaler.transform(probe))[0])
            if not _np.isfinite(prediction):
                raise ValueError("smoke-test prediction not finite")
            # Atomic swap (single assignments; readers see old or new, never a mix)
            self.model = new_model
            self.scaler = new_scaler
            self.is_trained = True
            logger.info(f"[SIMILARITY_MODEL] ✅ Runtime model reloaded from artifact")
            return True
        except Exception as e:
            logger.error(f"[SIMILARITY_MODEL] Runtime reload failed (previous model kept): {e}")
            return False

    def get_status(self) -> Dict:
        """Get model status and statistics"""
        return {
            'is_trained': self.is_trained,
            'model_path': self.model_path,
            'training_samples': len(self.training_samples),
            'model_available': self.model is not None,
            'sklearn_available': SKLEARN_AVAILABLE
        }


# Global instance
similarity_model = SimilarityModel()

