# Documentation Index

**Face Recognition Surveillance System**  
**ITDIR-AI DEPARTMENT**

---

## 📚 Quick Navigation

All documentation files are organized with numbered prefixes for easy navigation.

---

## ⚠️ Start Here If You Call the API (v6.0.0 — July 2026)

The platform hardening release changed several API contracts. Before upgrading
any client or script, read:

1. **50_API_DOCUMENTATION.md** → *Platform-Wide Conventions* (CSRF, structured
   errors, background jobs, pagination) and the **Migration Checklist (v5 → v6)**
   at the end of the chapter.
2. **In-app tutorial**: Admin → Tutorial → *"Platform Hardening: What Changed"* —
   the live version always matches the running build.

**Headlines:** cookie-authenticated mutations now require an
`X-Requested-With: XMLHttpRequest` header · expensive operations (relationship
calculation, threshold learning, model training, alert channel tests) return
`202 + job_id` instead of blocking · model training produces a reviewable
*candidate* rather than replacing the live model · watchlist deletion is now
reversible (soft delete + restore) · the social-network graph is always bounded ·
generated map HTML must be embedded in a sandboxed iframe.

---

## 🚀 Getting Started (01-02)

- **01_QUICK_START.md** - Fastest way to get the system running
- **02_DOCKER_QUICK_START.md** - Quick start using Docker

---

## ⚙️ Installation & Setup (03-05)

- **03_ADMIN_SETUP_GUIDE.md** - How to set up admin users
- **04_SETUP_NVIDIA_DOCKER.md** - GPU setup for Docker
- **05_MIGRATION_GUIDE.md** - Database migration instructions

---

## 👤 User Guides (06-08)

- **06_PROMOTE_AND_MERGE_GUIDE.md** - How to promote and merge identities
- **07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md** - Complete guide to Unknown Faces Center
- **08_IDENTITY_API_FRONTEND_GUIDE.md** - How to use identity features in the frontend

---

## 🔧 System Features (09-11)

- **09_HOW_MERGE_SUGGESTIONS_WORK.md** - Detailed explanation of merge suggestions
- **10_AUTO_CLEAN_AND_CLUSTER_JOBS_GUIDE.md** - Automated background jobs
- **11_SYSTEM_CAPABILITIES.md** - Complete system capabilities overview

---

## 📖 Technical Documentation (12-17)

- **12_README.md** - Main system documentation
- **13_README_GPU.md** - GPU support guide
- **14_PRODUCTION_README.md** - Production deployment guide
- **15_PERFORMANCE_OPTIMIZATION.md** - Performance tuning guide
- **16_PERSISTENCE_STATUS.md** - Data persistence information
- **17_CAPACITY_VERIFICATION.md** - System capacity verification

---

## 🔍 Troubleshooting & Reference (18-28)

- **18_AUDIT_LOGGING_GUIDE.md** - Audit logging system
- **19_BLOCKED_USERS.md** - User blocking system
- **20_NAVBAR_COMPONENT_GUIDE.md** - Frontend navbar component
- **21_WEBHOOK_TROUBLESHOOTING.md** - Webhook troubleshooting
- **22_WEBHOOK_DEBUG.md** - Webhook debugging guide
- **23_CLEANUP_UNKNOWN_IDENTITIES_GUIDE.md** - Cleanup procedures
- **24_SETTINGS_MANAGEMENT_GUIDE.md** - Complete guide to managing system settings via web interface
- **25_API_AUTHENTICATION_GUIDE.md** - Complete guide to API authentication and token usage
- **26_USER_PIPELINE_ACCESS_GUIDE.md** - User pipeline access and identity management for regular users
- **27_HOW_TO_GRANT_UNKNOWN_FACES_ACCESS.md** - Step-by-step guide on how to grant users access to Unknown Faces page
- **28_MULTI_IDENTITY_MERGE_GUIDE.md** - Complete guide to merging multiple identities efficiently with smart target selection

## 🎯 Advanced Features (29-45)

- **30_FAISS_PRODUCTION_SCALING.md** - FAISS scaling strategies for production
- **31_DYNAMIC_PROMOTION_FLOW.md** - How dynamic promotion works
- **32_50_CAMERAS_SCALABILITY_ANALYSIS.md** - Scalability analysis for 50+ cameras
- **33_FAISS_REPAIR_AND_SYNCHRONIZATION.md** - Complete guide to FAISS index repair and synchronization system
- **34_SCRFD_ARCFACE_INTEGRATION_PIPELINE.md** - Complete step-by-step integration guide for SCRFD and ArcFace models
- **35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md** - Complete debugging guide for identity recognition failures
- **36_CONFIGURATION_GUIDE.md** - Complete configuration guide with all settings explained in simple terms
- **37_ADVANCED_MERGE_FLOW_GUIDE.md** - Complete advanced guide to multi-pipeline identity merge flow with detailed examples and technical deep dive
- **38_SEARCH_BY_IMAGE_GUIDE.md** - Complete guide to searching for identities using face images with step-by-step flow and API reference
- **39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md** - Production-grade advanced search with multi-face detection, watchlists, live alerts, and intelligence features
- **40_LIVE_ALERTS_GUIDE.md** - Complete guide to live search alerts - real-time monitoring and notifications when tracked individuals are detected
- **41_PIPELINE_AWARE_ML_CLUSTERING_GUIDE.md** - Pipeline-aware ML clustering for merge suggestions - user-specific, pipeline-filtered suggestions with ML-based similarity
- **42_ML_SIMILARITY_MODEL_GUIDE.md** - Trainable neural network for merge suggestions - learns from user feedback to improve accuracy over time
- **45_SECURITY_INTELLIGENCE_GUIDE.md** - Security intelligence features and threat analysis

## 🗺️ Map & Tracking Intelligence (46-49)

- **46_MAP_SERVICE_GUIDE.md** - Complete guide to backend map generation service with security intelligence features
- **47_MAP_SERVICE_DATA_FLOW.md** - Data flow and integration documentation for map service
- **48_SECURITY_INTELLIGENCE_MAP_FEATURES.md** - Security intelligence features for maps (pattern detection, risk scoring, threat visualization)
- **49_MAP_SERVICE_PRODUCTION_GUIDE.md** - Production deployment guide for map service with monitoring and optimization

## 📚 API & Tutorials (50-51)

- **50_API_DOCUMENTATION.md** - Complete API reference (v6.0.0: platform-wide conventions, watchlist/ML-lifecycle/SQL-agent/live-alert endpoints, migration checklist)
- **51_TUTORIAL_GUIDE.md** - Step-by-step tutorials for common tasks

---

## 🧠 System Deep-Dives (64-66)

- **64_IDENTITY_RECOGNITION_EXPLANATION.md** - Complete explanation of how the identity recognition system works end-to-end
- **65_IMAGE_QUALITY_ANALYSIS.md** - How image quality is analyzed inside the detection pipeline
- **66_IMAGE_SECURITY_ANALYSIS.md** - Security analysis of image serving methods (direct URL vs Base64)

---

## 🚀 Enhancements & Verification (67-68)

- **67_SNA_ENHANCEMENTS_QUICK_START.md** - 5-minute quick start for the advanced SNA (Social Network Analysis) enhancements
- **68_WORKFLOW_VERIFICATION_REPORT.md** - End-to-end workflow verification report with confirmed status

---

## 🧹 Database Maintenance (69)

- **69_CLEAR_DATABASE_GUIDE.md** - Commands to safely clear database data in the `face_recognition_db` container

---

## 🎯 Most Common Documents

**For New Users:**
1. Start with: **01_QUICK_START.md**
2. Then read: **06_PROMOTE_AND_MERGE_GUIDE.md**
3. Learn: **07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md**
4. Search: **38_SEARCH_BY_IMAGE_GUIDE.md** - Find people by uploading a photo
5. Merge: **28_MULTI_IDENTITY_MERGE_GUIDE.md** - How to merge identities with preview

**For Administrators:**
1. Setup: **03_ADMIN_SETUP_GUIDE.md**
2. Features: **09_HOW_MERGE_SUGGESTIONS_WORK.md**
3. System: **11_SYSTEM_CAPABILITIES.md**
4. Settings: **36_CONFIGURATION_GUIDE.md** - All settings explained
5. Merge: **37_ADVANCED_MERGE_FLOW_GUIDE.md** - Production-grade merge with preview
6. Search: **39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md** - Watchlists, alerts, intelligence

**For Developers:**
1. Architecture: **12_README.md**
2. Performance: **15_PERFORMANCE_OPTIMIZATION.md**
3. API: **08_IDENTITY_API_FRONTEND_GUIDE.md**
4. FAISS: **33_FAISS_REPAIR_AND_SYNCHRONIZATION.md**
5. Merge: **37_ADVANCED_MERGE_FLOW_GUIDE.md** - Deep dive into merge API & FAISS
6. Search: **39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md** - Advanced search API & schemas

**For Troubleshooting:**
1. Webhooks: **21_WEBHOOK_TROUBLESHOOTING.md**
2. General: **22_WEBHOOK_DEBUG.md**
3. Identity: **35_IDENTITY_RECOGNITION_DEBUG_GUIDE.md**
4. Search: **38_SEARCH_BY_IMAGE_GUIDE.md** - Search troubleshooting included

---

## 📝 Documentation Updates

**Last Updated:** July 2026  
**Total Documents:** 101 organized files  
**Status:** All duplicates removed, organized with numbered prefixes; root-level docs merged into Docs/ (64-69)  
**New Features:** Production-grade merge with preview, AI target selection, type promotion, search by image, advanced search intelligence, live alerts with backend-driven defaults, pipeline-aware ML clustering, trainable similarity model

---

## 🔗 Quick Links

- **Tutorial Endpoint**: `/admin/tutorial` (in web interface)
- **API Documentation**: `/docs` (Swagger UI)
- **Main README**: See **12_README.md**

---

**Need Help?** Check the tutorial endpoint in the admin panel for step-by-step guides!

