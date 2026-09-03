# Relational ML model families

The security-intelligence MLOps pipeline supports four governed model
families. Rules remain authoritative; training never deploys a model.

| Model | Entity | Dataset | Algorithms | Output | Serving boundary |
|---|---|---|---|---|---|
| `behavior_anomaly_model` | person | unsupervised person snapshots | Isolation Forest, MAD | anomaly score/band | approved shadow |
| `coappearance_anomaly_model` | canonical identity pair | unsupervised pair snapshots | Isolation Forest, MAD | anomaly score/band | approved on-demand shadow |
| `social_graph_anomaly_model` | graph node/person | unsupervised graph snapshots | Isolation Forest, MAD | anomaly score/band | approved on-demand shadow |
| `threat_ranking_model` | person | reviewed-label supervised snapshots | logistic regression, random forest, gradient boosting | relative review-rank score | offline analyst prioritisation |

## Data contracts

Pair ids are sorted UUIDs joined by `|`. Pair features describe frequency,
rate, shared cameras, percentage, relationship span and recency. Social-graph
features describe normalized degree, weighted degree, weighted PageRank,
local clustering, bridge ratio and mean incident edge weight. Graph vectors
are emitted only after the configured node, edge and observation-span floors
pass. Missing evidence is recorded as unavailable; it is never converted to
zero.

Relational snapshots observe the relationship cache at collection time. The
system does not reconstruct an old graph from current mutable edges. Repeated
collection builds the immutable history required for temporal evaluation.

Threat ranking consumes only active, reviewed, manual positive/negative
labels joined to a snapshot at or before the label event. Its classifier
output is used for ordering analyst work. Until an independent calibration
study exists, it is explicitly `risk_rank_score`, `is_probability=false`,
`calibration_status=not_calibrated`.

## Operational flow

1. Run feature collection; it captures person, pair and graph snapshots.
2. Build the model's typed dataset definition.
3. Submit training through the durable ML worker.
4. Review the immutable dataset, evaluation, artifact hash and engineering
   gates in MLOps.
5. Approve anomaly candidates to shadow only. Threat ranking remains offline.

Admin-only observational APIs:

- `POST /api/ml/score/relational`
- `POST /api/ml/rank/threat-review`

Both responses include `applied_to_live_result=false`. Neither endpoint
creates or changes a threat assessment, alert, watchlist membership or
decision mode.
