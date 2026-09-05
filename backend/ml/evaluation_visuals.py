"""Descriptive supervised diagnostics; never consumed by readiness or serving."""


def supervised_visuals(model, feature_names, test_matrix, test_labels):
    import numpy as np
    from sklearn.metrics import confusion_matrix

    report = {}
    if len(test_labels):
        scores = model.predict_proba(test_matrix)[:, 1]
        report["confusion_matrix"] = confusion_matrix(
            test_labels, scores >= 0.5, labels=[0, 1]).tolist()
        report["confusion_matrix_context"] = {
            "split": "test", "labels": ["negative", "positive"], "threshold": 0.5,
            "meaning": "Diagnostic classifier threshold on reviewed labels only. This is not a threat probability or a production decision threshold."}
    importance = getattr(model, "feature_importances_", None)
    if importance is not None:
        report["feature_importance"] = dict(zip(feature_names, map(float, importance)))
        report["feature_importance_method"] = "Impurity reduction; descriptive and potentially biased toward variable features, not causal influence."
    elif hasattr(model, "coef_"):
        report["feature_importance"] = dict(zip(feature_names, map(float, model.coef_[0])))
        report["feature_importance_method"] = "Signed logistic coefficients in original feature units. Magnitudes across different units are not directly comparable."
    loss = getattr(model, "train_score_", None)
    if loss is not None:
        report["training_curves"] = [{"iteration": i + 1, "training_loss": float(v)}
                                     for i, v in enumerate(loss) if np.isfinite(v)]
        report["training_curves_context"] = "Training loss recorded by the fitted boosting model. No validation learning curve was recorded."
    return report
