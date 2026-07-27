"""
Agent Tools Module
==================
Tools/nodes for the SQL Intelligence Agent workflow.
"""

import re
import json
import logging
from typing import List, Dict, Optional
from difflib import SequenceMatcher

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser

from ..config import config
from ..state import AgentState
from ..llm import create_llm, create_sql_llm
from ..database import DatabaseManager
from ..knowledge_base import SQLKnowledgeBase
from .sql_tools import prepare_sql_from_llm_response, validate_sql_query

# Setup logger for SQL Agent Tools
logger = logging.getLogger(__name__)


class SQLAgentTools:
    """Tools for the SQL Intelligence Agent."""

    def __init__(self, conversation_memory=None):
        self.llm = create_llm()  # fast general model: chat, intent, normalization, analysis
        self.sql_llm = create_sql_llm()  # SQL specialist: query generation and repair only
        self.db = DatabaseManager(config)
        self.kb = SQLKnowledgeBase(config)  # Knowledge Base for RAG
        self.conversation_memory = conversation_memory  # Conversation memory for context

    def detect_malicious_intent(self, state: AgentState) -> AgentState:
        """
        STEP 0: Malicious Intent Detection
        Scan user input for database alteration intent BEFORE any processing.
        This is the FIRST security layer that catches malicious queries immediately.
        """
        logger.info(f"[STEP_0] Security scan - Input: {state['original_input'][:100]}")
        print("\n" + "="*60)
        print("🛡️ STEP 0: SECURITY SCAN - MALICIOUS INTENT DETECTION")
        print("="*60)
        print(f"📥 Scanning input: {state['original_input']}")
        
        user_input_lower = state['original_input'].lower()
        
        # Patterns that indicate malicious intent (database alteration)
        # Made more comprehensive to catch all variations
        malicious_patterns = [
            # DELETE operations - catch ANY mention of delete
            (r'\bdelete\b', 'DELETE operation'),
            (r'\bremove\s+(all|everything|records|data|from|database|table)\b', 'DELETE operation'),
            (r'\bclear\s+(all|everything|table|database|data|records)\b', 'DELETE operation'),
            (r'\bwipe\s+(out|all|everything|database|data)\b', 'DELETE operation'),
            (r'\berase\s+(all|everything|database|data|records)\b', 'DELETE operation'),
            (r'\bpurge\s+(all|everything|database|data|records)\b', 'DELETE operation'),
            
            # UPDATE operations - catch ANY mention of update/modify/change/edit
            (r'\bupdate\b', 'UPDATE operation'),
            (r'\bmodify\s+(all|everything|table|records|data|database)\b', 'UPDATE operation'),
            (r'\bchange\s+(all|everything|table|records|data|database)\b', 'UPDATE operation'),
            (r'\bedit\s+(all|everything|table|records|data|database)\b', 'UPDATE operation'),
            (r'\breplace\s+(all|everything|table|records|data)\b', 'UPDATE operation'),
            
            # INSERT operations - catch ANY mention of insert/add/create
            (r'\binsert\b', 'INSERT operation'),
            (r'\badd\s+(new|fake|test|dummy|record|data)\s+(to|into)\s+(table|database)\b', 'INSERT operation'),
            (r'\bcreate\s+(new|fake|test|dummy|record|data|row)\b', 'INSERT operation'),
            
            # ALTER operations - catch ANY mention of alter
            (r'\balter\b', 'ALTER operation'),
            (r'\balter\s+table\b', 'ALTER TABLE operation'),
            (r'\bmodify\s+table\b', 'ALTER TABLE operation'),
            (r'\bchange\s+table\b', 'ALTER TABLE operation'),
            
            # DROP/TRUNCATE operations
            (r'\bdrop\b', 'DROP operation'),
            (r'\btruncate\b', 'TRUNCATE operation'),
            (r'\bdestroy\s+(table|database|all|everything)\b', 'DROP operation'),
            
            # SQL injection patterns
            (r';\s*(delete|update|insert|drop|alter|truncate)', 'SQL injection attempt'),
            (r'union\s+.*\s+(delete|update|insert|drop|alter)', 'SQL injection attempt'),
            (r'exec\s*\(|execute\s*\(', 'SQL injection attempt'),
            
            # Permission modification
            (r'\bgrant\b', 'Permission modification'),
            (r'\brevoke\b', 'Permission modification'),
            
            # Transaction control
            (r'\bcommit\b', 'Transaction control'),
            (r'\brollback\b', 'Transaction control'),
            (r'\bbegin\s+transaction\b', 'Transaction control'),
        ]
        
        # Check for malicious patterns
        detected_operations = []
        for pattern, operation in malicious_patterns:
            if re.search(pattern, user_input_lower, re.IGNORECASE):
                detected_operations.append(operation)
                logger.error(f"[SECURITY] ⚠️ STEP 0: Detected malicious intent - {operation}")
                logger.error(f"[SECURITY] Pattern matched: {pattern}")
                logger.error(f"[SECURITY] User input: {state['original_input']}")
        
        if detected_operations:
            # CRITICAL: User attempted malicious operation - BLOCK IMMEDIATELY
            operations_str = ", ".join(set(detected_operations))
            block_reason = f"Attempted database alteration operation detected in natural language query: {operations_str}. Query: {state['original_input'][:200]}"
            
            logger.error(f"[SECURITY] 🚨 STEP 0: BLOCKING USER - Malicious intent detected: {operations_str}")
            logger.error(f"[SECURITY] Block reason: {block_reason}")
            
            # Mark user for blocking
            # Try multiple ways to get user_id
            user_id = getattr(self.conversation_memory, 'user_id', None)
            if not user_id and hasattr(self.conversation_memory, 'user_id'):
                user_id = self.conversation_memory.user_id
            
            # Always set security flags even if user_id is not available
            # The API route will handle the actual blocking when it receives these flags
            logger.error(f"[SECURITY] ⚠️ STEP 0: Marking user for blocking - Malicious intent in natural language")
            if user_id:
                logger.error(f"[SECURITY] User ID available: {user_id}")
            else:
                logger.warning(f"[SECURITY] User ID not available in conversation_memory - will be handled by API route")
            
            state["security_block_user"] = True
            state["security_block_reason"] = block_reason
            # Store user_id in state if available for API route to use
            if user_id:
                state["security_block_user_id"] = user_id
            
            # Set error state to stop processing
            state["error"] = f"SECURITY VIOLATION: {operations_str} detected. This system is read-only and cannot execute data modification operations."
            state["final_response"] = f"SECURITY ALERT: Your query was blocked because it attempts to modify the database ({operations_str}). This system is read-only. Your account has been blocked. Please contact an administrator immediately."
            state["query_result"] = {
                "success": False,
                "error": f"Security: {operations_str} operations are not allowed. Your account has been blocked.",
                "rows": [],
                "row_count": 0
            }
            
            print(f"🚨 SECURITY ALERT: Malicious intent detected!")
            print(f"   Operations: {operations_str}")
            print(f"   User will be blocked immediately")
            print(f"   Processing stopped")
            
            return state
        
        print(f"✅ Security scan passed - No malicious intent detected")
        logger.debug(f"[STEP_0] Security scan passed")
        return state

    def fix_language(self, state: AgentState) -> AgentState:
        """
        STEP 1: Language Normalization
        Correct grammar, spelling, and clarity.
        """
        logger.info(f"[STEP_1] Language normalization - Input: {state['original_input'][:100]}")
        print("\n" + "="*60)
        print("🔧 STEP 1: LANGUAGE NORMALIZATION")
        print("="*60)
        print(f"📥 Original Input: {state['original_input']}")

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content="""You are a language correction assistant.
Your task is to fix ONLY grammar and spelling errors in the user's input.

STRICT RULES:
- NEVER add new words or information that wasn't in the original
- NEVER expand abbreviations or add assumed context
- NEVER add last names, titles, or any assumed information
- If a name is given (like "Joey"), keep it exactly as "Joey" - do NOT add surnames
- Only fix actual typos and grammar errors
- If the input is already clear, return it EXACTLY as-is
- Return ONLY the corrected text, nothing else

Example:
- "track joey" → "Track Joey" (only capitalize, don't add anything)
- "show me usres" → "Show me users" (fix typo only)
- "whre is john" → "Where is John" (fix typo and capitalize)"""),
            HumanMessage(content=f"Fix this input (DO NOT add any new words): {state['original_input']}")
        ])

        chain = prompt | self.llm | StrOutputParser()

        try:
            normalized = chain.invoke({})
            normalized = normalized.strip()

            # Safety check: if LLM added significantly more words, revert to original
            original_words = len(state['original_input'].split())
            normalized_words = len(normalized.split())

            if normalized_words > original_words + 1:  # Allow max 1 extra word (for minor fixes)
                print(f"⚠️ LLM added words! Reverting to original.")
                print(f"   Original: '{state['original_input']}' ({original_words} words)")
                print(f"   Normalized: '{normalized}' ({normalized_words} words)")
                state["normalized_input"] = state["original_input"]
            else:
                state["normalized_input"] = normalized

            print(f"📤 Normalized Input: {state['normalized_input']}")
            logger.debug(f"[STEP_1] Normalized: {state['normalized_input']}")
        except Exception as e:
            state["normalized_input"] = state["original_input"]
            state["error"] = f"Language normalization error: {str(e)}"
            logger.error(f"[STEP_1] Language normalization failed: {str(e)}", exc_info=True)
            print(f"❌ Error: {state['error']}")

        return state

    def correct_name_typos(self, state: AgentState) -> AgentState:
        """
        STEP 1.5: Name Correction & Typo Handling
        Check for typos in person names and suggest corrections from database.
        """
        logger.info("[STEP_1.5] Starting name correction and typo handling")
        print("\n" + "="*60)
        print("🔍 STEP 1.5: NAME CORRECTION & TYPO HANDLING")
        print("="*60)

        normalized = state.get("normalized_input", "")
        
        # Extract potential person names from the query
        # Look for patterns like "Track X", "Where is X", "Find X", etc.
        name_patterns = [
            r'(?:track|find|where is|locate|show|who is)\s+([A-Z][a-z]+)',
            r'(?:track|find|where is|locate|show|who is)\s+([a-z]+)',
        ]
        
        potential_names = []
        for pattern in name_patterns:
            matches = re.findall(pattern, normalized, re.IGNORECASE)
            potential_names.extend(matches)
        
        if not potential_names:
            logger.debug("[STEP_1.5] No person names detected in query")
            print("✅ No person names detected in query")
            return state
        
        logger.info(f"[STEP_1.5] Detected potential names: {potential_names}")
        print(f"🔍 Detected potential names: {potential_names}")
        
        # Get all known names from database
        try:
            logger.debug("[STEP_1.5] Querying database for known names")
            conn = self.db.get_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT name
                    FROM faces
                    WHERE name IS NOT NULL AND name != ''
                    ORDER BY name
                    LIMIT 500
                """)
                known_names = [row[0] for row in cur.fetchall()]
            conn.close()
            
            logger.info(f"[STEP_1.5] Found {len(known_names)} known names in database")
            print(f"📋 Found {len(known_names)} known names in database")
            
            corrections = {}
            for potential_name in potential_names:
                potential_name_lower = potential_name.lower()
                best_match = None
                best_similarity = 0.0
                
                # Find best matching name
                for known_name in known_names:
                    similarity = SequenceMatcher(None, potential_name_lower, known_name.lower()).ratio()
                    if similarity > best_similarity and similarity >= 0.6:  # 60% similarity threshold
                        best_similarity = similarity
                        best_match = known_name
                
                if best_match and best_match.lower() != potential_name_lower:
                    corrections[potential_name] = best_match
                    print(f"   ✏️ '{potential_name}' → '{best_match}' (similarity: {best_similarity:.2f})")
            
            # Apply corrections
            if corrections:
                corrected_input = normalized
                for old_name, new_name in corrections.items():
                    # Case-insensitive replacement
                    corrected_input = re.sub(
                        re.escape(old_name),
                        new_name,
                        corrected_input,
                        flags=re.IGNORECASE
                    )
                
                state["normalized_input"] = corrected_input
                state["name_corrections"] = corrections
                logger.info(f"[STEP_1.5] Applied name corrections: {corrections}")
                print(f"✅ Applied corrections: {corrections}")
                print(f"📤 Corrected input: {corrected_input}")
                
                # Add correction notice to response context
                correction_notice = f"Note: Corrected name(s): {', '.join([f'{old} → {new}' for old, new in corrections.items()])}"
                state["name_correction_notice"] = correction_notice
            else:
                logger.debug("[STEP_1.5] No corrections needed - names match database")
                print("✅ No corrections needed - names match database")
                
        except Exception as e:
            logger.warning(f"[STEP_1.5] Error checking names: {str(e)}", exc_info=True)
            print(f"⚠️ Error checking names: {e}")
            # Continue with original input if name check fails
            # This is not critical, so we don't set an error state
        
        return state

    def classify_intent(self, state: AgentState) -> AgentState:
        """
        STEP 2: Intent Classification
        Classify as CHAT, SQL_QUERY, or HYBRID.
        """
        logger.info(f"[STEP_2] Classifying intent for: {state['normalized_input'][:100]}")
        print("\n" + "="*60)
        print("🎯 STEP 2: INTENT CLASSIFICATION")
        print("="*60)
        print(f"📥 Input to classify: {state['normalized_input']}")

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content="""You are an intent classifier for a SQL database assistant.

Classify the user's intent as one of:
- CHAT: General conversation, explanations, or questions not requiring database access
- SQL_QUERY: Requires querying the database to answer
- HYBRID: Needs both explanation and database query

Respond with ONLY a JSON object in this exact format:
{"intent": "CHAT" or "SQL_QUERY" or "HYBRID", "confidence": 0.0 to 1.0}

IMPORTANT: Any mention of a person's name with verbs like "track", "find", "locate", "show", "where is"
should be classified as SQL_QUERY because it requires searching the database.

Examples:
- "What is SQL?" → {"intent": "CHAT", "confidence": 0.95}
- "Show me all users" → {"intent": "SQL_QUERY", "confidence": 0.95}
- "Explain what customers we have" → {"intent": "HYBRID", "confidence": 0.85}
- "Track Joey" → {"intent": "SQL_QUERY", "confidence": 0.95}
- "Where is John" → {"intent": "SQL_QUERY", "confidence": 0.95}
- "Find person named Sarah" → {"intent": "SQL_QUERY", "confidence": 0.95}
- "Who was detected today" → {"intent": "SQL_QUERY", "confidence": 0.95}"""),
            HumanMessage(content=f"Classify this: {state['normalized_input']}")
        ])

        chain = prompt | self.llm | StrOutputParser()

        try:
            logger.info("[STEP_2] 🤖 Calling LLM for intent detection...")
            logger.debug(f"[STEP_2] Intent prompt being sent to LLM: {prompt.messages[-1].content[:500]}...")
            result = chain.invoke({})
            logger.info(f"[STEP_2] ✅ LLM intent response received ({len(result)} chars)")
            logger.info(f"[STEP_2] 📝 LLM INTENT RESPONSE:\n{result}\n{'='*80}")
            print(f"🤖 LLM Raw Response: {result}")
            # Parse JSON from response
            json_match = re.search(r'\{[^}]+\}', result)
            if json_match:
                parsed = json.loads(json_match.group())
                state["intent"] = parsed.get("intent", "CHAT")
                state["intent_confidence"] = float(parsed.get("confidence", 0.5))
                print(f"📤 Intent: {state['intent']} (confidence: {state['intent_confidence']})")
            else:
                state["intent"] = "CHAT"
                state["intent_confidence"] = 0.5
                print(f"⚠️ Could not parse JSON, defaulting to CHAT")
        except Exception as e:
            state["intent"] = "CHAT"
            state["intent_confidence"] = 0.5
            state["error"] = f"Intent classification error: {str(e)}"
            logger.error(f"[STEP_2] Intent classification failed: {str(e)}", exc_info=True)
            print(f"❌ Error: {state['error']}")

        return state

    def check_schema(self, state: AgentState) -> AgentState:
        """
        STEP 3: Schema Awareness
        Load and provide schema information.
        """
        logger.info("[STEP_3] Loading database schema")
        print("\n" + "="*60)
        print("📋 STEP 3: SCHEMA AWARENESS")
        print("="*60)

        try:
            state["schema_description"] = self.db.get_schema_description()
            logger.info(f"[STEP_3] Schema loaded successfully ({len(state['schema_description'])} chars)")
            print(f"✅ Schema loaded ({len(state['schema_description'])} chars)")
            print(f"📄 Tables: faces, detections, pipelines, system_metrics")
        except Exception as e:
            state["schema_description"] = ""
            state["error"] = f"Schema loading error: {str(e)}"
            logger.error(f"[STEP_3] Schema loading failed: {str(e)}", exc_info=True)
            print(f"❌ Error: {state['error']}")

        return state

    def retrieve_examples(self, state: AgentState) -> AgentState:
        """
        STEP 3.5: RAG Retrieval
        Search knowledge base for similar questions and their SQL queries.
        """
        logger.info(f"[STEP_3.5] RAG retrieval for: {state['normalized_input'][:100]}")
        print("\n" + "="*60)
        print("📚 STEP 3.5: RAG RETRIEVAL")
        print("="*60)
        print(f"🔍 Searching for: {state['normalized_input']}")

        try:
            # Search for similar questions
            # Scoped to the caller: learned examples carry the raw text of
            # the question that produced them, and retrieved examples are
            # interpolated into the SQL-generation system prompt. Unscoped,
            # one user's questions became another user's prompt content.
            examples = self.kb.search_similar(
                query=state["normalized_input"],
                top_k=config.rag_top_k,
                user_id=state.get("user_id"),
            )

            state["retrieved_examples"] = examples
            state["rag_context"] = self.kb.format_examples_for_prompt(examples)

            if examples:
                logger.info(f"[STEP_3.5] Found {len(examples)} similar examples")
                print(f"✅ Found {len(examples)} similar examples:")
                for i, ex in enumerate(examples, 1):
                    logger.debug(f"[STEP_3.5] Example {i}: {ex['question']} (similarity: {ex['similarity']})")
                    print(f"   {i}. \"{ex['question']}\" (similarity: {ex['similarity']})")
            else:
                logger.warning("[STEP_3.5] No similar examples found")
                print("⚠️ No similar examples found")

        except Exception as e:
            state["retrieved_examples"] = []
            state["rag_context"] = ""
            state["error"] = f"RAG retrieval error: {str(e)}"
            logger.error(f"[STEP_3.5] RAG retrieval failed: {str(e)}", exc_info=True)
            print(f"❌ Error: {state['error']}")

        return state

    def generate_sql(self, state: AgentState) -> AgentState:
        """
        STEP 4: SQL Generation (RAG-enhanced)
        Generate safe, optimized SQL queries using retrieved examples.
        Uses the prepare_sql_from_llm_response tool to clean up the output.
        """
        logger.info(f"[STEP_4] Generating SQL for: {state['normalized_input'][:100]}")
        print("\n" + "="*60)
        print("⚙️ STEP 4: SQL GENERATION")
        print("="*60)
        print(f"📥 Query to generate SQL for: {state['normalized_input']}")

        # Build RAG context
        rag_section = ""
        if state.get("rag_context"):
            rag_section = f"""
REFERENCE EXAMPLES:
Use these similar examples from the knowledge base as guidance.
Adapt them to match the user's specific question.

{state['rag_context']}
"""
            print(f"📚 RAG context included: {len(state['rag_context'])} chars")
        else:
            print("⚠️ No RAG context available")

        # Get conversation context if available
        conversation_context = state.get("conversation_context", "")
        context_section = ""
        if conversation_context:
            context_section = f"""
CONVERSATION CONTEXT:
{conversation_context}

Use this context to understand references to previous queries (e.g., "that person", "the same camera", etc.).
"""
        
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=f"""You are an expert SQL query generator for PostgreSQL.

{state.get('schema_description', 'No schema available')}
{rag_section}
{context_section}

CRITICAL SECURITY RULES - READ-ONLY MODE:
=========================================
⚠️⚠️⚠️ ABSOLUTE PROHIBITION - NO EXCEPTIONS ⚠️⚠️⚠️

This SQL agent operates in STRICT READ-ONLY mode. You MUST follow these rules ABSOLUTELY:

ALLOWED OPERATIONS (ONLY - NO EXCEPTIONS):
- SELECT queries ONLY
- WITH clauses (Common Table Expressions) that contain ONLY SELECT statements
- EXPLAIN queries (for query analysis)

FORBIDDEN OPERATIONS (NEVER USE - EVEN IN CTEs, COMMENTS, OR SIMULATIONS):
- DELETE (NEVER, even in CTEs, comments, or "simulation" queries)
- UPDATE (NEVER, even in CTEs or comments)
- INSERT (NEVER, even in CTEs or comments)
- MERGE, UPSERT, REPLACE (NEVER)
- DROP, ALTER, CREATE, RENAME, TRUNCATE (NEVER)
- GRANT, REVOKE (NEVER)
- EXEC, EXECUTE, CALL (NEVER)
- COMMIT, ROLLBACK, BEGIN, START TRANSACTION (NEVER)
- COPY (NEVER)

CRITICAL INSTRUCTIONS:
1. If a user requests DELETE, UPDATE, INSERT, or any data modification, you MUST respond with:
   {{"sql": "", "purpose": "This system is read-only. Data modification operations (DELETE, UPDATE, INSERT) are not permitted. Please contact an administrator if you need to modify data."}}

2. DO NOT generate queries that simulate deletion, even if the user asks for it
3. DO NOT use DELETE, UPDATE, or INSERT keywords anywhere in your response, even in comments
4. DO NOT create CTEs that contain DELETE logic
5. If you see "delete", "update", "insert" in the user's request, immediately return empty SQL with an explanation

VIOLATION OF THESE RULES WILL RESULT IN IMMEDIATE USER BLOCKING AND ADMIN NOTIFICATION.

Query Generation Rules:
1. Generate READ-ONLY queries ONLY (SELECT, WITH, EXPLAIN)
2. NEVER use any DML (Data Manipulation Language) or DDL (Data Definition Language) operations
3. Use explicit column names (avoid SELECT *)
4. Add appropriate filtering, aggregation, and LIMIT clauses
5. Optimize for clarity and performance
6. Learn from the reference examples but adapt to the specific question
7. If the request cannot be fulfilled with the schema, explain why

Respond with ONLY a JSON object:
{{"sql": "YOUR SQL QUERY HERE", "purpose": "Brief explanation of what the query does"}}

If no query is possible:
{{"sql": "", "purpose": "Explanation of why no query can be generated"}}"""),
            HumanMessage(content=f"Generate SQL for: {state['normalized_input']}")
        ])

        chain = prompt | self.sql_llm | StrOutputParser()

        try:
            logger.info("[STEP_4] 🤖 Calling LLM for SQL generation...")
            logger.debug(f"[STEP_4] Prompt being sent to LLM: {prompt.messages[-1].content[:500]}...")
            print("🤖 Calling LLM for SQL generation...")
            result = chain.invoke({})
            logger.info(f"[STEP_4] ✅ LLM raw response received ({len(result)} chars)")
            logger.info(f"[STEP_4] 📝 LLM RAW RESPONSE:\n{result}\n{'='*80}")
            print(f"🤖 LLM Raw Response:\n{result}")
            print("-"*40)

            # Use the prepare_sql_from_llm_response tool to parse and clean the response
            print("🔧 Using prepare_sql_from_llm_response tool...")
            prepared = prepare_sql_from_llm_response.invoke(result)

            if prepared["success"]:
                generated_sql = prepared["sql"]
                
                # SECURITY LAYER 0: IMMEDIATE validation after SQL generation
                validation = self.db._validate_query(generated_sql)
                if not validation["is_safe"]:
                    logger.error(f"[SECURITY] ⚠️ LAYER 0: Forbidden SQL detected immediately after generation: {validation['reason']}")
                    logger.error(f"[SECURITY] Generated SQL: {generated_sql[:500]}")
                    print(f"🚨 SECURITY ALERT: {validation['reason']}")
                    
                    # Mark user for blocking
                    user_id = getattr(self.conversation_memory, 'user_id', None)
                    if user_id:
                        logger.error(f"[SECURITY] ⚠️ LAYER 0: Marking user ID {user_id} for blocking - Forbidden SQL in generated query")
                        state["security_block_user"] = True
                        state["security_block_reason"] = f"Forbidden SQL detected in generated query: {validation['reason']}. SQL: {generated_sql[:200]}"
                    
                    # Set error state
                    state["generated_sql"] = ""
                    state["sql_purpose"] = f"SECURITY VIOLATION: {validation['reason']}. This system is read-only and cannot execute data modification operations."
                    state["query_result"] = {
                        "success": False,
                        "error": validation["reason"] + " Your account will be blocked. Please contact an administrator.",
                        "rows": [],
                        "row_count": 0
                    }
                    logger.error(f"[STEP_4] SQL generation blocked by security validation")
                    print(f"❌ SECURITY: Query blocked - {validation['reason']}")
                    return state
                
                state["generated_sql"] = generated_sql
                state["sql_purpose"] = prepared["purpose"]
                logger.info(f"[STEP_4] SQL generated successfully (length: {len(generated_sql)} chars)")
                logger.debug(f"[STEP_4] Generated SQL: {generated_sql[:200]}...")
                logger.debug(f"[STEP_4] Transformations: {prepared['transformations']}")
                print(f"✅ SQL prepared successfully!")
                print(f"📝 Transformations applied: {prepared['transformations']}")
                print(f"📤 Generated SQL:\n{state['generated_sql']}")
                print(f"📝 Purpose: {state['sql_purpose']}")
            else:
                state["generated_sql"] = ""
                state["sql_purpose"] = prepared["error"]
                logger.error(f"[STEP_4] SQL preparation failed: {prepared['error']}")
                print(f"❌ Could not prepare SQL: {prepared['error']}")

        except Exception as e:
            state["generated_sql"] = ""
            state["sql_purpose"] = f"SQL generation error: {str(e)}"
            logger.error(f"[STEP_4] SQL generation exception: {str(e)}", exc_info=True)
            print(f"❌ Error: {state['sql_purpose']}")

        return state

    def validate_and_fix_sql(self, state: AgentState) -> AgentState:
        """
        STEP 4.5: SQL Validation and Error Fixing
        Validates the generated SQL and fixes any errors before execution.
        Uses LLM tool calling pattern to validate and correct SQL.
        """
        logger.info("[STEP_4.5] Starting SQL validation")
        print("\n" + "="*60)
        print("🔍 STEP 4.5: SQL VALIDATION & FIXING")
        print("="*60)

        generated_sql = state.get("generated_sql", "")

        if not generated_sql:
            logger.warning("[STEP_4.5] No SQL to validate")
            print("❌ No SQL to validate")
            state["sql_validation_status"] = "ERROR"
            state["sql_validation_error"] = "No SQL to validate"
            return state

        logger.debug(f"[STEP_4.5] Validating SQL ({len(generated_sql)} chars)")
        print(f"📥 SQL to validate:\n{generated_sql}")

        # Use the validate_sql_query tool
        validation_result = validate_sql_query.invoke(generated_sql)

        if validation_result["is_valid"]:
            logger.info("[STEP_4.5] SQL validation passed")
            print("✅ SQL passed validation")
            state["sql_validation_status"] = "VALID"
            state["sql_fixes_applied"] = []
            state["sql_validation_warnings"] = validation_result.get("warnings", [])
            if state["sql_validation_warnings"]:
                logger.warning(f"[STEP_4.5] Validation warnings: {state['sql_validation_warnings']}")
                print(f"⚠️ Warnings: {state['sql_validation_warnings']}")
            return state

        logger.warning(f"[STEP_4.5] Validation errors found: {validation_result['errors']}")
        print(f"⚠️ Validation errors found: {validation_result['errors']}")

        # Try to fix the SQL using LLM
        print("\n🔧 Attempting to fix SQL with LLM...")

        fix_prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=f"""You are an expert PostgreSQL SQL debugger and fixer.

DATABASE SCHEMA:
{state.get('schema_description', 'No schema available')}

You received a SQL query that has potential issues. Your task is to:
1. Analyze the SQL query for errors
2. Fix ALL issues while maintaining the original intent
3. Ensure the query is syntactically correct for PostgreSQL
4. Ensure all column and table names match the schema
5. Add proper quotes/escaping where needed

VALIDATION ERRORS DETECTED:
{chr(10).join(f"- {e}" for e in validation_result['errors'])}

RULES:
- Return ONLY valid PostgreSQL SELECT/WITH/EXPLAIN queries
- Preserve the original query intent
- Fix syntax errors, missing keywords, incorrect escaping
- Ensure proper JOIN conditions
- CRITICAL: detections.pipeline_id (varchar) must join to pipelines.pipeline_id (varchar), NOT pipelines.id (integer)
- The correct join is: JOIN pipelines p ON d.pipeline_id = p.pipeline_id
- Ensure column names exist in the referenced tables
- Add missing aliases where needed
- Fix string concatenation issues (use proper escaping)

Respond with ONLY a JSON object:
{{"fixed_sql": "YOUR CORRECTED SQL QUERY", "fixes_applied": ["list", "of", "fixes"], "is_valid": true/false}}

If the query cannot be fixed:
{{"fixed_sql": "", "fixes_applied": [], "is_valid": false, "error": "explanation"}}"""),
            HumanMessage(content=f"""Fix this SQL query:

ORIGINAL SQL:
{generated_sql}

USER'S ORIGINAL REQUEST: {state.get('normalized_input', '')}

Provide the corrected SQL:""")
        ])

        chain = fix_prompt | self.sql_llm | StrOutputParser()

        try:
            logger.info("[STEP_5] 🤖 Calling LLM for SQL fix...")
            logger.debug(f"[STEP_5] Fix prompt being sent to LLM: {fix_prompt.messages[-1].content[:500]}...")
            result = chain.invoke({})
            logger.info(f"[STEP_5] ✅ LLM fix response received ({len(result)} chars)")
            logger.info(f"[STEP_5] 📝 LLM FIX RESPONSE:\n{result}\n{'='*80}")
            print(f"🤖 LLM Fix Response:\n{result}")

            # Use prepare_sql_from_llm_response to parse the fix response
            prepared = prepare_sql_from_llm_response.invoke(result)

            if prepared["success"] and prepared["sql"]:
                fixed_sql = prepared["sql"]

                # Re-validate the fixed SQL
                revalidation = validate_sql_query.invoke(fixed_sql)

                if revalidation["is_valid"]:
                    state["generated_sql"] = fixed_sql
                    state["sql_validation_status"] = "FIXED"
                    state["sql_fixes_applied"] = prepared["transformations"]
                    print(f"✅ SQL successfully fixed!")
                    print(f"📝 Transformations: {prepared['transformations']}")
                    print(f"📤 Fixed SQL:\n{fixed_sql}")
                else:
                    print(f"⚠️ Fixed SQL still has issues, keeping original")
                    state["sql_validation_status"] = "PARTIAL"
                    state["sql_validation_warnings"] = revalidation["errors"]
            else:
                print(f"❌ Could not fix SQL: {prepared['error']}")
                state["sql_validation_status"] = "INVALID"
                state["sql_validation_error"] = prepared["error"]

        except Exception as e:
            print(f"❌ Error during SQL fixing: {str(e)}")
            state["sql_validation_status"] = "ERROR"
            state["sql_validation_error"] = str(e)

        return state

    def prepare_sql_for_execution(self, state: AgentState) -> AgentState:
        """
        STEP 4.6: Final SQL Preparation
        Clean and prepare the SQL for safe execution.
        Also performs early read-only validation.
        """
        print("\n" + "="*60)
        print("📋 STEP 4.6: PREPARING SQL FOR EXECUTION")
        print("="*60)

        generated_sql = state.get("generated_sql", "")

        if not generated_sql:
            print("❌ No SQL to prepare")
            return state

        # Early validation: Check if query is read-only before cleanup
        validation = self.db._validate_query(generated_sql)
        if not validation["is_safe"]:
            logger.warning(f"[STEP_4.6] Early validation failed: {validation['reason']}")
            print(f"❌ Security validation failed: {validation['reason']}")
            
            # SECURITY LAYER 2: Mark user for blocking (will be handled in API route)
            user_id = getattr(self.conversation_memory, 'user_id', None)
            if user_id:
                logger.error(f"[SECURITY] ⚠️ LAYER 2: Marking user ID {user_id} for blocking - Early validation failed: {validation['reason']}")
                state["security_block_user"] = True
                state["security_block_reason"] = f"Attempted forbidden SQL operation: {validation['reason']}. Query: {generated_sql[:200]}"
            else:
                logger.warning(f"[SECURITY] Validation failed but no user_id available: {validation['reason']}")
            
            state["query_result"] = {
                "success": False,
                "error": validation["reason"] + " Your account has been blocked. Please contact an administrator.",
                "rows": [],
                "row_count": 0
            }
            return state

        # Use the prepare_sql_from_llm_response tool for final cleanup
        # (This ensures consistent formatting)
        mock_json = json.dumps({"sql": generated_sql, "purpose": state.get("sql_purpose", "")})
        prepared = prepare_sql_from_llm_response.invoke(mock_json)

        if prepared["success"]:
            state["generated_sql"] = prepared["sql"]
            # Validate again after cleanup to ensure no malicious code was introduced
            final_validation = self.db._validate_query(prepared["sql"])
            if not final_validation["is_safe"]:
                logger.warning(f"[STEP_4.6] Post-cleanup validation failed: {final_validation['reason']}")
                print(f"❌ Security validation failed after cleanup: {final_validation['reason']}")
                
                # SECURITY LAYER 3: Mark user for blocking (will be handled in API route)
                user_id = getattr(self.conversation_memory, 'user_id', None)
                if user_id:
                    logger.error(f"[SECURITY] ⚠️ LAYER 3: Marking user ID {user_id} for blocking - Post-cleanup validation failed: {final_validation['reason']}")
                    state["security_block_user"] = True
                    state["security_block_reason"] = f"Attempted forbidden SQL operation after cleanup: {final_validation['reason']}. Query: {prepared['sql'][:200]}"
                else:
                    logger.warning(f"[SECURITY] Post-cleanup validation failed but no user_id available: {final_validation['reason']}")
                
                state["query_result"] = {
                    "success": False,
                    "error": final_validation["reason"] + " Your account has been blocked. Please contact an administrator.",
                    "rows": [],
                    "row_count": 0
                }
                return state
            print(f"✅ SQL ready for execution (read-only validated)")
            print(f"📤 Final SQL:\n{prepared['sql']}")
        else:
            print(f"✅ SQL ready for execution (no additional cleanup needed)")

        return state

    def execute_sql(self, state: AgentState) -> AgentState:
        """
        STEP 5: SQL Execution
        Execute the generated SQL safely.
        """
        logger.info("[STEP_5] Executing SQL query")
        print("\n" + "="*60)
        print("🚀 STEP 5: SQL EXECUTION")
        print("="*60)

        if not state.get("generated_sql"):
            logger.error("[STEP_5] No SQL query to execute")
            print("❌ No SQL query to execute!")
            print(f"   generated_sql = '{state.get('generated_sql')}'")
            state["query_result"] = {
                "success": False,
                "error": "No SQL query to execute",
                "rows": [],
                "row_count": 0
            }
            return state

        logger.debug(f"[STEP_5] Executing SQL: {state['generated_sql'][:200]}...")
        print(f"📤 Executing SQL:\n{state['generated_sql']}")

        try:
            result = self.db.execute_query(state["generated_sql"])
            state["query_result"] = result
            if result.get("success"):
                row_count = result.get('row_count', 0)
                logger.info(f"[STEP_5] Query executed successfully - {row_count} rows returned")
                print(f"✅ Query successful! Rows returned: {row_count}")
                if result.get("rows"):
                    logger.debug(f"[STEP_5] Sample data: {result['rows'][:3]}")
                    print(f"📊 Sample data: {result['rows'][:3]}")
            else:
                error = result.get('error', 'Unknown error')
                logger.error(f"[STEP_5] Query execution failed: {error}")
                print(f"❌ Query failed: {error}")
                
                # SECURITY LAYER 4: Mark user for blocking (will be handled in API route)
                if "Security:" in error or "forbidden" in error.lower() or "read-only" in error.lower():
                    user_id = getattr(self.conversation_memory, 'user_id', None)
                    if user_id:
                        logger.error(f"[SECURITY] ⚠️ LAYER 4: Marking user ID {user_id} for blocking - Execution validation failed: {error}")
                        state["security_block_user"] = True
                        state["security_block_reason"] = f"Attempted forbidden SQL operation during execution: {error}. Query: {state['generated_sql'][:200]}"
                    else:
                        logger.warning(f"[SECURITY] Execution validation failed but no user_id available: {error}")
        except Exception as e:
            logger.error(f"[STEP_5] SQL execution exception: {str(e)}", exc_info=True)
            state["query_result"] = {
                "success": False,
                "error": str(e),
                "rows": [],
                "row_count": 0
            }
            print(f"❌ Exception: {str(e)}")

        return state

    def generate_story_response(self, state: AgentState) -> AgentState:
        """
        STEP 6: Humanized Story Output
        Transform results into a clear narrative.
        """
        logger.info("[STEP_6] Generating story response")
        print("\n" + "="*60)
        print("📖 STEP 6: STORY RESPONSE GENERATION")
        print("="*60)

        intent = state.get("intent", "CHAT")
        logger.debug(f"[STEP_6] Intent: {intent}")
        print(f"📥 Intent: {intent}")

        if intent == "CHAT":
            # Pure chat response
            prompt = ChatPromptTemplate.from_messages([
                SystemMessage(content="""You are a helpful database assistant.
Answer the user's question in a friendly, professional manner.
Do not expose internal workings or SQL queries."""),
                HumanMessage(content=state["normalized_input"])
            ])
        else:
            # SQL-based response
            query_result = state.get("query_result", {})

            if not query_result.get("success"):
                error_msg = query_result.get("error", "Unknown error occurred")
                state["final_response"] = f"I encountered an issue while trying to retrieve that information: {error_msg}"
                return state

            rows = query_result.get("rows", [])
            row_count = query_result.get("row_count", 0)

            if row_count == 0:
                state["final_response"] = "I searched the database but found no matching records for your query."
                return state

            # Check if this is a person tracking query (has camera_name/pipeline_id and timestamp)
            is_tracking_query = False
            if rows:
                first_row = rows[0]
                has_camera = any(key in first_row for key in ['camera_name', 'pipeline_id', 'camera'])
                has_timestamp = any(key in first_row for key in ['timestamp', 'time'])
                has_name = any(key in first_row for key in ['name', 'person_name'])
                
                if has_camera and has_timestamp and has_name:
                    is_tracking_query = True

            # Process and format results for LLM with actual data extraction
            # Extract key information from actual data
            processed_data = self._process_tracking_data(rows) if is_tracking_query else rows
            results_preview = json.dumps(processed_data[:100], indent=2, default=str)  # Increased limit

            if is_tracking_query:
                # Extract actual statistics from data
                actual_stats = self._extract_tracking_stats(rows)
                
                # Natural Narrative Surveillance Intelligence Report prompt
                prompt = ChatPromptTemplate.from_messages([
                    SystemMessage(content="""You are an elite Surveillance Intelligence Analyst crafting compelling, detailed intelligence narratives from a Security Intelligence System. Your reports read like gripping surveillance intelligence stories that bring the data to life.

CRITICAL DATA USAGE RULES:
- You MUST use ONLY the actual data provided in the "Detection Results" section
- You MUST extract camera names, timestamps, and locations EXACTLY as they appear in the data
- NEVER invent or create fake camera names like "Camera A", "Camera B", "Main Entrance", etc.
- NEVER use placeholder values like [X], [Y], [duration], [location], [time]
- ALWAYS use the exact camera/pipeline names from the data (e.g., "MD5AL_3EIN_7LWE", "IT-DIR-MD5AL")
- ALWAYS use actual timestamps from the data in the exact format provided
- ALWAYS use the ACTUAL PERSON'S NAME from the data - NEVER use "subject" or "the subject"
- ALWAYS calculate real statistics from the actual data provided
- If a field is missing in the data, state "Data not available" rather than inventing values

REPORT STYLE - Natural Intelligence Narrative:
- Write as a compelling surveillance intelligence story, like a professional intelligence briefing
- Use natural, flowing English that reads like a narrative story
- Write in first-person or third-person narrative style (e.g., "Ross was detected..." not "The subject was detected...")
- Use the person's actual name throughout the entire report - NEVER refer to them as "subject" or "the subject"
- Create vivid, detailed descriptions that paint a picture of the surveillance activity
- Use descriptive language that makes the intelligence come alive
- Include rich details about timing, locations, movements, and patterns
- Write with the style of an elite intelligence analyst telling a story

REPORT STRUCTURE - Natural Narrative Format:
Write a flowing narrative report that includes:

1. OPENING: Start with a compelling opening that introduces the person by name and sets the scene
   Example: "Intelligence tracking for Ross reveals a detailed surveillance pattern across multiple detection points..."

2. CHRONOLOGICAL NARRATIVE: Tell the story chronologically, weaving together:
   - Exact timestamps and camera locations
   - The person's name (not "subject")
   - Detailed descriptions of each detection
   - Movement patterns and time between detections
   - Confidence levels and what they indicate
   Example: "At 12:12:48, Ross was first detected at camera MD5AL_3EIN_7LWE with a confidence level of 42.3%. The detection occurred..."

3. MOVEMENT ANALYSIS: Describe the movement pattern as a story:
   - Where Ross started
   - How Ross moved between locations
   - Time spent at each location
   - Any patterns or anomalies observed
   Example: "Ross's movement pattern shows..."

4. STATISTICAL INSIGHTS: Weave statistics naturally into the narrative:
   - Total detections
   - Unique locations visited
   - Average confidence levels
   - Time spans and durations
   - Present these as part of the story, not as a separate table

5. KEY INTELLIGENCE: Highlight important observations naturally:
   - Notable behaviors or patterns
   - Unusual activities
   - Significant time gaps or movements
   - Confidence level variations

6. ASSESSMENT: Provide intelligence assessment in narrative form:
   - What the patterns suggest
   - Potential significance
   - Any concerns or notable aspects

7. RECOMMENDATIONS: Conclude with actionable intelligence recommendations in natural language

LANGUAGE STYLE:
- Natural, flowing English - like telling a story
- Use the person's name throughout (Ross, Joey, etc.) - NEVER "subject" or "the subject"
- Descriptive and vivid - paint a picture with words
- Professional but engaging - like an elite intelligence analyst
- Include specific details: exact times, camera names, confidence levels
- Use varied sentence structure for readability
- Create a sense of narrative flow and progression
- Write as a compelling story, not structured sections or tables

EXAMPLE NARRATIVE STYLE:
Write as a flowing narrative story. Here's an example:

"Intelligence tracking for Ross reveals a detailed surveillance pattern across our detection network. At precisely 12:12:48, Ross was first detected at camera MD5AL_3EIN_7LWE with a confidence match of 42.3%. This initial detection marked the beginning of our surveillance window for Ross's activities.

The detection occurred at camera MD5AL_3EIN_7LWE, where Ross appeared with a moderate confidence level of 42.3%. This single detection point provides limited movement data, as Ross was observed only once during the surveillance period. The detection suggests Ross was present at this specific location at that exact moment in time.

Analysis of the detection pattern shows that Ross maintained a stationary position at camera MD5AL_3EIN_7LWE, with no subsequent movements detected across our surveillance network. The single detection point indicates Ross was present for a brief moment, with a confidence level that suggests a positive match to our facial recognition database.

Statistical analysis reveals that Ross was detected once across our entire surveillance network, appearing at a single detection point (MD5AL_3EIN_7LWE) with an average confidence level of 42.3%. The detection occurred on 2026-01-01 at 12:12:48, representing the first and only recorded appearance of Ross during this surveillance period.

Key intelligence observations indicate that Ross's appearance was isolated to a single location, with no movement patterns detected between multiple cameras. The moderate confidence level of 42.3% suggests a reasonable match, though additional detections would strengthen the identification confidence.

Based on this single detection point, our intelligence assessment indicates limited actionable intelligence. Without multiple detection points or movement patterns, we cannot determine Ross's complete route, dwell times, or behavioral patterns. Further surveillance and monitoring are recommended to gather additional intelligence on Ross's activities and potential connections to other surveillance targets.

Recommendations include continued monitoring of camera MD5AL_3EIN_7LWE and expansion of surveillance coverage to adjacent detection points to capture any future appearances by Ross. Enhanced monitoring protocols should be implemented to increase the likelihood of multiple detection points, which would provide more comprehensive intelligence on Ross's movement patterns and activities."

CRITICAL RULES:
- ALWAYS use the person's actual name (Ross, Joey, etc.) - NEVER "subject" or "the subject"
- Write as a flowing narrative story, NOT structured sections or tables
- Include rich details and vivid descriptions
- Pair every timestamp with the exact camera name from data
- Use natural English - like telling a compelling story
- Calculate real statistics and weave them naturally into the narrative
- Never use tables, structured formats, or formal report sections
- Make it engaging and compelling like an elite intelligence analyst's narrative report"""),
                    HumanMessage(content=f"""User asked: {state['normalized_input']}

Query purpose: {state.get('sql_purpose', 'Track person movement')}

ACTUAL DETECTION RESULTS ({row_count} total detections):
{results_preview}

EXTRACTED STATISTICS FROM ACTUAL DATA:
{actual_stats}

Generate a compelling, natural narrative-style SURVEILLANCE INTELLIGENCE REPORT using ONLY the actual data provided above. 

CRITICAL REQUIREMENTS:
- Use the ACTUAL PERSON'S NAME from the data throughout the entire report - NEVER use "subject" or "the subject"
- Write in natural, flowing English like telling a story
- Extract camera names EXACTLY as they appear in the data (look for camera_name, pipeline_id fields)
- Use timestamps EXACTLY as they appear in the data
- ALWAYS pair every time mention with the camera name: "At 12:12:48 at camera MD5AL_3EIN_7LWE, Ross was detected..."
- Write chronologically, weaving together timestamps, locations, and the person's name naturally
- Include rich details and vivid descriptions that bring the surveillance activity to life
- Calculate real statistics from the actual data and weave them naturally into the narrative
- Do NOT invent or create fake camera names or locations
- Do NOT use tables or structured formats - write as a flowing narrative story
- Include all actual detections in chronological order with camera names
- Provide detailed analysis based on the actual movement patterns in the data
- Make every time reference clear by including the camera location
- Write like an elite intelligence analyst telling a compelling surveillance story
- Use descriptive language: "Ross appeared at camera MD5AL_3EIN_7LWE at precisely 12:12:48..."
- Include confidence levels naturally: "with a confidence match of 42.3%"
- Describe movements vividly: "Ross moved from camera X to camera Y, covering the distance in..."
- Format the report for easy reading with clear sections and consistent time/camera pairing""")
                ])
            else:
                # Professional Surveillance Intelligence Report for general queries
                prompt = ChatPromptTemplate.from_messages([
                    SystemMessage(content="""You are a Security Intelligence Analyst generating professional surveillance reports from a Security Intelligence System. Your reports must be formal, precise, and intelligence-focused.

CRITICAL DATA USAGE RULES:
- You MUST use ONLY the actual data provided in the "Results" section
- You MUST extract all values EXACTLY as they appear in the data
- NEVER invent or create fake values, names, or statistics
- NEVER use placeholder values like [X], [Y], [number], [time range], [percentage]
- ALWAYS calculate real statistics from the actual data provided
- ALWAYS use exact field names and values from the data
- If a field is missing in the data, state "Data not available" rather than inventing values

REPORT FORMAT - Advanced Intelligence Briefing:
- Write as a formal intelligence report from a security system
- Use professional, authoritative language appropriate for security operations
- Structure as an advanced intelligence briefing with deep analysis
- Include all critical operational details with precise data
- Maintain objectivity and factual accuracy
- Present data in a clear, actionable format with quantitative analysis

REPORT STRUCTURE:
1. HEADER: "SURVEILLANCE INTELLIGENCE REPORT" or "INTELLIGENCE BRIEFING"
2. EXECUTIVE SUMMARY: Brief overview with actual statistics from data
3. KEY FINDINGS: Main data points with exact values from the data
4. DETAILED ANALYSIS: Intelligence analysis with quantitative metrics
5. STATISTICAL INSIGHTS: Calculated statistics and patterns from actual data
6. ASSESSMENT: Operational assessment with data-driven recommendations

LANGUAGE STYLE:
- Professional, formal, and authoritative
- Use advanced security/intelligence terminology
- Active voice, direct statements
- Precise references with exact data values
- Objective observations with quantitative analysis
- Data-driven conclusions with statistical backing
- Clear, concise, and actionable

TERMINOLOGY:
- "Detection events" for face detections
- "Observation points" or "Detection points" for cameras (use actual names from data)
- "Confidence levels" for recognition quality (convert similarity to percentage)
- "Operational metrics" for system statistics
- "Activity patterns" for behavioral trends
- Use formal time references with actual timestamps
- Present statistics clearly with exact numbers

CRITICAL RULES:
- ALWAYS use formal report structure
- ALWAYS extract and use EXACT values from the data provided
- ALWAYS calculate real statistics from the actual data (count rows, sum values, calculate averages, etc.)
- NEVER use placeholder values or invented data
- NEVER use generic examples - use the actual data provided
- ALWAYS use professional security terminology
- NEVER mention technical database terms (use descriptive names instead)
- ALWAYS maintain objectivity and factual reporting
- ALWAYS structure as an advanced intelligence briefing
- Include quantitative analysis and statistical insights from the actual data"""),
                    HumanMessage(content=f"""User asked: {state['normalized_input']}

Query purpose: {state.get('sql_purpose', 'Data retrieval')}

ACTUAL RESULTS ({row_count} total rows):
{results_preview}

Generate a professional SURVEILLANCE INTELLIGENCE REPORT using ONLY the actual data provided above.
- Extract all values EXACTLY as they appear in the data
- Calculate real statistics from the actual data (count, sum, average, etc.)
- Do NOT invent or create fake values, names, or statistics
- Use exact field names and values from the data
- Present all findings with precise data points from the actual results
- Provide advanced analysis based on the actual patterns in the data""")
                ])

        chain = prompt | self.llm | StrOutputParser()

        try:
            logger.debug("[STEP_6] Calling LLM for story generation")
            
            # Check if we're in streaming mode (has streaming_callback)
            streaming_callback = state.get("streaming_callback")
            
            if streaming_callback:
                # Stream word-by-word
                logger.debug("[STEP_6] Using streaming mode for word-by-word generation")
                response_text = ""
                buffer = ""
                
                # Stream tokens from LLM
                for chunk in chain.stream({}):
                    if chunk:
                        buffer += chunk
                        # Split by spaces to get words, but keep punctuation with words
                        words = buffer.split(' ')
                        # If we have complete words (more than one word or buffer ends with space)
                        if len(words) > 1 or buffer.endswith(' '):
                            # Send all complete words except the last one (which might be incomplete)
                            for word in words[:-1]:
                                if word.strip():
                                    response_text += word + ' '
                                    # Call the callback with each word
                                    streaming_callback({"type": "content", "content": word + ' ', "step": "response"})
                            # Keep the last word in buffer (might be incomplete)
                            buffer = words[-1] if not buffer.endswith(' ') else ""
                
                # Send any remaining buffer
                if buffer.strip():
                    response_text += buffer
                    streaming_callback({"type": "content", "content": buffer, "step": "response"})
                
                response_text = response_text.strip()
            else:
                # Non-streaming mode (original behavior)
                logger.info("[STEP_6] 🤖 Calling LLM for story generation (non-streaming)...")
                logger.debug(f"[STEP_6] Story prompt being sent to LLM: {prompt.messages[-1].content[:500]}...")
                response = chain.invoke({})
                logger.info(f"[STEP_6] ✅ LLM story response received ({len(response)} chars)")
                logger.info(f"[STEP_6] 📝 LLM STORY RESPONSE:\n{response}\n{'='*80}")
                response_text = response.strip()
                
                # Even in non-streaming mode, send the response via callback if available
                # This ensures the frontend receives the content
                if streaming_callback:
                    logger.debug("[STEP_6] Sending non-streaming response via callback")
                    # Send response in chunks for better UX
                    chunk_size = 100
                    for i in range(0, len(response_text), chunk_size):
                        chunk = response_text[i:i+chunk_size]
                        streaming_callback({"type": "content", "content": chunk, "step": "response"})
            
            # Add name correction notice if applicable
            if state.get("name_correction_notice"):
                notice = state['name_correction_notice'] + '\n\n'
                if streaming_callback:
                    # Stream the notice word-by-word too
                    for word in notice.split(' '):
                        if word.strip():
                            streaming_callback({"type": "content", "content": word + ' ', "step": "response"})
                response_text = notice + response_text
            
            state["final_response"] = response_text
            logger.info(f"[STEP_6] Story response generated successfully ({len(state['final_response'])} chars)")
            logger.debug(f"[STEP_6] Response preview: {state['final_response'][:200]}...")
            print(f"✅ Final response generated ({len(state['final_response'])} chars)")
        except Exception as e:
            logger.error(f"[STEP_6] Story generation failed: {str(e)}", exc_info=True)
            error_msg = f"I apologize, but I encountered an error while preparing your response: {str(e)}"
            state["final_response"] = error_msg
            if streaming_callback:
                streaming_callback({"type": "error", "message": error_msg, "step": "error"})
            print(f"❌ Error generating response: {str(e)}")

        print("\n" + "="*60)
        print("🏁 FINAL RESPONSE:")
        print("="*60)
        print(state["final_response"])
        print("="*60)

        return state

    def _process_tracking_data(self, rows: list) -> list:
        """Process tracking data to ensure all fields are properly formatted."""
        processed = []
        for row in rows:
            processed_row = {}
            # Map common field names
            for key, value in row.items():
                # Normalize field names
                if key in ['pipeline_id', 'camera']:
                    processed_row['camera_name'] = value
                elif key in ['detection_time', 'time']:
                    processed_row['timestamp'] = value
                elif key in ['person_name', 'subject']:
                    processed_row['name'] = value
                else:
                    processed_row[key] = value
            processed.append(processed_row)
        return processed

    def _extract_tracking_stats(self, rows: list) -> dict:
        """Extract actual statistics from tracking data."""
        if not rows:
            return {}
        
        from datetime import datetime
        
        stats = {
            "total_detections": len(rows),
            "unique_cameras": set(),
            "unique_names": set(),
            "timestamps": [],
            "first_detection": None,
            "last_detection": None,
            "total_duration_seconds": None,
            "confidence_scores": []
        }
        
        for row in rows:
            # Extract camera name
            camera = row.get('camera_name') or row.get('pipeline_id') or row.get('camera')
            if camera:
                stats["unique_cameras"].add(str(camera))
            
            # Extract name
            name = row.get('name') or row.get('person_name')
            if name:
                stats["unique_names"].add(str(name))
            
            # Extract timestamp
            timestamp = row.get('timestamp') or row.get('detection_time') or row.get('time')
            if timestamp:
                try:
                    if isinstance(timestamp, str):
                        # Try parsing different timestamp formats
                        for fmt in ['%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S']:
                            try:
                                ts = datetime.strptime(timestamp, fmt)
                                stats["timestamps"].append(ts)
                                break
                            except:
                                continue
                    else:
                        stats["timestamps"].append(timestamp)
                except:
                    pass
            
            # Extract confidence/similarity
            confidence = row.get('similarity') or row.get('confidence')
            if confidence is not None:
                try:
                    stats["confidence_scores"].append(float(confidence))
                except:
                    pass
        
        # Calculate statistics
        stats["unique_cameras"] = list(stats["unique_cameras"])
        stats["unique_names"] = list(stats["unique_names"])
        stats["camera_count"] = len(stats["unique_cameras"])
        stats["name_count"] = len(stats["unique_names"])
        
        if stats["timestamps"]:
            stats["timestamps"].sort()
            stats["first_detection"] = stats["timestamps"][0]
            stats["last_detection"] = stats["timestamps"][-1]
            if len(stats["timestamps"]) > 1:
                duration = (stats["last_detection"] - stats["first_detection"]).total_seconds()
                stats["total_duration_seconds"] = duration
                stats["total_duration_minutes"] = duration / 60
                stats["total_duration_hours"] = duration / 3600
        
        if stats["confidence_scores"]:
            stats["avg_confidence"] = sum(stats["confidence_scores"]) / len(stats["confidence_scores"])
            stats["min_confidence"] = min(stats["confidence_scores"])
            stats["max_confidence"] = max(stats["confidence_scores"])
        
        # Format for display
        formatted_stats = {
            "Total Detections": stats["total_detections"],
            "Unique Detection Points": stats["camera_count"],
            "Detection Points": ", ".join(stats["unique_cameras"]) if stats["unique_cameras"] else "N/A",
            "Subjects Tracked": ", ".join(stats["unique_names"]) if stats["unique_names"] else "N/A",
        }
        
        if stats["first_detection"]:
            formatted_stats["First Detection"] = stats["first_detection"].strftime("%Y-%m-%d %H:%M:%S")
        if stats["last_detection"]:
            formatted_stats["Last Detection"] = stats["last_detection"].strftime("%Y-%m-%d %H:%M:%S")
        if stats["total_duration_seconds"]:
            hours = int(stats["total_duration_seconds"] // 3600)
            minutes = int((stats["total_duration_seconds"] % 3600) // 60)
            seconds = int(stats["total_duration_seconds"] % 60)
            formatted_stats["Total Tracking Duration"] = f"{hours}h {minutes}m {seconds}s"
        
        if stats.get("avg_confidence"):
            formatted_stats["Average Confidence"] = f"{stats['avg_confidence']*100:.1f}%"
            formatted_stats["Confidence Range"] = f"{stats['min_confidence']*100:.1f}% - {stats['max_confidence']*100:.1f}%"
        
        return formatted_stats

    def handle_chat(self, state: AgentState) -> AgentState:
        """Handle pure chat responses without SQL."""
        print("\n" + "="*60)
        print("💬 CHAT RESPONSE (No SQL needed)")
        print("="*60)
        print(f"📥 Input: {state['normalized_input']}")

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content="""You are a helpful database assistant.
Answer questions about databases, SQL, and data in a friendly manner.
If you don't know something, say so politely."""),
            HumanMessage(content=state["normalized_input"])
        ])

        chain = prompt | self.llm | StrOutputParser()

        try:
            logger.info("[STEP_7] 🤖 Calling LLM for final response generation (chat mode)...")
            logger.debug(f"[STEP_7] Final response prompt being sent to LLM: {prompt.messages[-1].content[:500]}...")
            response = chain.invoke({})
            logger.info(f"[STEP_7] ✅ LLM final response received ({len(response)} chars)")
            logger.info(f"[STEP_7] 📝 LLM FINAL RESPONSE:\n{response}\n{'='*80}")
            state["final_response"] = response.strip()
            logger.info(f"[STEP_7] ✅ Final response set in state ({len(state['final_response'])} chars)")
            logger.info(f"[STEP_7] 📤 FINAL RESPONSE CONTENT:\n{state['final_response']}\n{'='*80}")
            print(f"✅ Chat response generated")
        except Exception as e:
            state["final_response"] = f"I apologize, but I encountered an error: {str(e)}"
            print(f"❌ Error: {str(e)}")

        print("\n" + "="*60)
        print("🏁 FINAL RESPONSE:")
        print("="*60)
        print(state["final_response"])
        print("="*60)

        return state

    def learn_from_query(self, state: AgentState) -> AgentState:
        """
        STEP 7: Learning
        If the query was successful, save it to the knowledge base for future reference.
        """
        logger.info("[STEP_7] Starting learning phase")
        print("\n" + "="*60)
        print("🧠 STEP 7: LEARNING")
        print("="*60)

        # Only learn from successful SQL queries
        query_result = state.get("query_result", {})
        generated_sql = state.get("generated_sql", "")

        query_success = query_result.get('success', False)
        has_sql = bool(generated_sql)
        should_learn = state.get('should_learn', True)
        
        logger.debug(f"[STEP_7] Query successful: {query_success}, Has SQL: {has_sql}, Should learn: {should_learn}")
        print(f"📊 Query successful: {query_success}")
        print(f"📊 Has SQL: {has_sql}")
        print(f"📊 Should learn: {should_learn}")

        if (
            query_result.get("success") and
            generated_sql and
            state.get("should_learn", True)
        ):
            try:
                # Check if this is a novel query worth learning
                existing = self.kb.search_similar(
                    state["normalized_input"],
                    top_k=1,
                    user_id=state.get("user_id"),
                )

                # Only learn if no very similar example exists (similarity < 0.9)
                if not existing or existing[0].get("similarity", 0) < 0.9:
                    self.kb.learn_from_success(
                        question=state["normalized_input"],
                        sql=generated_sql,
                        purpose=state.get("sql_purpose", ""),
                        user_id=state.get("user_id"),
                    )
                    logger.info(f"[STEP_7] Learned new query pattern: {state['normalized_input'][:100]}")
                    print("✅ Learned new query pattern!")
                else:
                    similarity = existing[0].get('similarity', 0)
                    logger.debug(f"[STEP_7] Skipped learning - similar example exists (similarity: {similarity})")
                    print(f"⏭️ Skipped learning - similar example exists (similarity: {similarity})")
            except Exception as e:
                # Don't fail the response if learning fails
                logger.warning(f"[STEP_7] Learning failed: {str(e)}", exc_info=True)
                print(f"⚠️ Learning failed: {str(e)}")
        else:
            print("⏭️ Skipped learning - conditions not met")

        return state
