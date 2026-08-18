"""ML pipeline (first release).

RULES remains the production decision system; this package builds the
foundation underneath it: point-in-time features, reviewed labels,
versioned datasets, an unsupervised behavioral-anomaly baseline, a gated
model registry, and shadow-only inference with honest comparison records.

Nothing here fabricates labels, probabilities, calibration, or performance.
Anomaly output is a behavioral signal, never a threat verdict.
"""
