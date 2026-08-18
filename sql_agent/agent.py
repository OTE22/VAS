"""
Agent Module
============
Main SQL Intelligence Agent class.
"""

import logging
from typing import List, Dict, Optional

from .config import config
from .state import AgentState
from .database import DatabaseManager
from .knowledge_base import SQLKnowledgeBase
from .conversation_memory import ConversationMemory
from .graph import create_sql_agent

# Setup logger for SQL Agent
logger = logging.getLogger(__name__)

# Neutral placeholder. Every transport replaces this with the policy layer's
# verdict, but if one ever forgets, the fallback the user sees must still be
# true — a refusal, never a claim about their account.
_SECURITY_PLACEHOLDER = ("That operation is not permitted — the assistant can "
                         "only read data.")


def _security_event(state: dict, reason: str) -> dict:
    """A DETECTION event: structured, and silent about account state.

    `security_violation` is what the transports key off. They must never key off
    the wording of `message` — that was the original defect: the API matched
    `message.startswith("Security:")` while the agent emitted "SECURITY ALERT:",
    so the policy layer was never invoked on streaming transports.
    """
    return {
        "type": "error",
        "step": "security_block",
        "security_violation": True,
        "security_reason_code": state.get("security_reason_code",
                                          "FORBIDDEN_SQL_ATTEMPT"),
        "security_reason": reason,
        "message": _SECURITY_PLACEHOLDER,
    }


class SQLIntelligenceAgent:
    """
    SQL Intelligence Agent

    An advanced agent that transforms natural language into meaningful
    database insights using a multi-step reasoning pipeline with RAG.
    """

    def __init__(self, conversation_memory: Optional[ConversationMemory] = None):
        logger.info("Initializing SQL Intelligence Agent")
        self.conversation_memory = conversation_memory or ConversationMemory()
        logger.info(f"Conversation memory initialized (session: {self.conversation_memory.current_session_id})")
        self.agent = create_sql_agent(conversation_memory=self.conversation_memory)
        self.db = DatabaseManager(config)
        self.kb = SQLKnowledgeBase(config)
        logger.info("SQL Intelligence Agent initialized successfully")

    def _create_initial_state(self, user_input: str, should_learn: bool = True) -> AgentState:
        """Create the initial state for the agent."""
        return {
            # Owner of this turn, taken from the per-user conversation memory.
            # Scopes knowledge-base retrieval and learning; when it is None the
            # knowledge base falls back to curated seed examples only.
            "user_id": getattr(self.conversation_memory, "user_id", None),
            "original_input": user_input,
            "normalized_input": "",
            "intent": "CHAT",
            "intent_confidence": 0.0,
            "schema_description": "",
            "retrieved_examples": [],
            "rag_context": "",
            "generated_sql": "",
            "sql_purpose": "",
            "sql_validation_status": "VALID",
            "sql_fixes_applied": [],
            "sql_validation_warnings": [],
            "sql_validation_error": None,
            "query_result": {},
            "final_response": "",
            "should_learn": should_learn,
            "error": None,
            "messages": [],
            "conversation_context": "",
            "name_corrections": None,
            "security_block_user": None,
            "security_block_reason": None
        }

    def query(self, user_input: str, learn: bool = True):
        """
        Process a user query and return a human-friendly response.

        Args:
            user_input: The natural language query from the user
            learn: Whether to save successful queries for future reference

        Returns:
            A human-readable narrative response, or tuple (response, result_dict) if security flags are set
        """
        logger.info(f"[SQL_AGENT] Processing query: {user_input[:100]}...")
        
        # Add user message to conversation memory
        self.conversation_memory.add_user_message(user_input)
        logger.debug(f"[SQL_AGENT] Added user message to conversation memory")
        
        # Get conversation context
        conversation_context = self.conversation_memory.get_conversation_context(limit=3)
        if conversation_context:
            logger.debug(f"[SQL_AGENT] Conversation context retrieved ({len(conversation_context)} chars)")
        
        initial_state = self._create_initial_state(user_input, should_learn=learn)
        initial_state["conversation_context"] = conversation_context

        # Run the agent
        try:
            logger.info("[SQL_AGENT] 🔄 Invoking agent workflow...")
            logger.debug(f"[SQL_AGENT] Initial state keys: {list(initial_state.keys())}")
            logger.debug(f"[SQL_AGENT] User input: {initial_state.get('original_input', 'N/A')}")
            
            result = self.agent.invoke(initial_state)
            
            logger.info(f"[SQL_AGENT] ✅ Agent workflow completed")
            logger.debug(f"[SQL_AGENT] Result keys: {list(result.keys())}")
            
            response = result.get("final_response", "I apologize, but I couldn't process your request.")
            
            logger.info(f"[SQL_AGENT] 📤 FINAL RESPONSE FROM AGENT ({len(response)} chars):")
            logger.info(f"[SQL_AGENT] {'='*80}")
            logger.info(f"[SQL_AGENT] {response}")
            logger.info(f"[SQL_AGENT] {'='*80}")
            
            # A violation was DETECTED. The API route runs it through the policy
            # layer, which decides — and states — what happens to the account.
            # This response is a placeholder the route replaces; it must not
            # claim an account action that may not occur.
            if result.get("security_block_user"):
                block_reason = result.get("security_block_reason", "Attempted forbidden SQL operation")
                user_id_from_state = result.get("security_block_user_id")  # Get user_id from state if available
                response = _SECURITY_PLACEHOLDER
                logger.error(f"[SECURITY] 🚨 Agent flagged a violation for policy review: {block_reason}")
                # Include user_id in result_dict if available
                if user_id_from_state:
                    result["security_block_user_id"] = user_id_from_state
                # Return both response and result for API route to check
                return response, result
            
            # Add AI response to conversation memory
            self.conversation_memory.add_ai_message(response)
            logger.info(f"[SQL_AGENT] ✅ Query processed successfully (response length: {len(response)} chars)")
            logger.debug(f"[SQL_AGENT] Response preview: {response[:200]}...")
            
            return response
        except Exception as e:
            logger.error(f"[SQL_AGENT] Error processing query: {str(e)}", exc_info=True)
            error_msg = f"I encountered an unexpected error: {str(e)}"
            self.conversation_memory.add_ai_message(error_msg)
            return error_msg

    def query_stream(self, user_input: str, learn: bool = True, cancel_event=None):
        """
        Process a user query and stream progress updates.

        Args:
            user_input: The natural language query from the user
            learn: Whether to save successful queries for future reference
            cancel_event: optional threading.Event — when set (client disconnect,
                timeout), the stream stops at the next graph-node boundary
                instead of running all remaining LLM calls

        Yields:
            Progress updates as the agent processes the query
        """
        logger.info(f"[SQL_AGENT] Processing query (streaming): {user_input[:100]}...")
        
        try:
            # Add user message to conversation memory
            self.conversation_memory.add_user_message(user_input)
            yield {"type": "status", "message": "Processing query...", "step": "start"}
            
            # Get conversation context
            conversation_context = self.conversation_memory.get_conversation_context(limit=3)
            initial_state = self._create_initial_state(user_input, should_learn=learn)
            initial_state["conversation_context"] = conversation_context
            
            # Set up streaming callback for word-by-word generation
            # Use a queue-like structure to collect words that need to be yielded
            word_queue = []
            
            def streaming_callback(data):
                """Callback to queue words for streaming"""
                if data.get("type") == "content":
                    word_queue.append(data)
            
            # Store callback in state (will be used by generate_story_response)
            initial_state["streaming_callback"] = streaming_callback
            
            # Stream the agent execution
            yield {"type": "status", "message": "Initializing agent workflow...", "step": "init"}
            
            # Use stream instead of invoke for streaming
            # Accumulate state as we go through the stream
            accumulated_state = initial_state.copy()
            final_response = None
            
            # Process all chunks from the stream
            stream_ended_early = False
            try:
                for chunk in self.agent.stream(initial_state):
                    # Cancellation (client disconnect / timeout): stop at the node
                    # boundary instead of running the remaining LLM calls.
                    if cancel_event is not None and cancel_event.is_set():
                        logger.info("[SQL_AGENT] 🛑 Stream cancelled - stopping at node boundary")
                        yield {"type": "complete", "message": "Query cancelled", "step": "done", "success": False}
                        return

                    # First, yield any queued words from streaming callback
                    while word_queue:
                        yield word_queue.pop(0)
                    
                    # Extract step information from chunk
                    for node_name, node_output in chunk.items():
                        # Update accumulated state with node output
                        accumulated_state.update(node_output)
                        
                        # A violation was DETECTED. What happens to the account is
                        # decided by sql_agent/security_policy.py, not here — so
                        # this event carries a structured marker and says nothing
                        # about account state. The transport replaces `message`
                        # with the policy's verdict before the client sees it.
                        #
                        # This used to yield "…Your account has been blocked."
                        # directly, while the policy layer was never reached at
                        # all on this transport: the user was told they were
                        # blocked on every attempt and never actually was.
                        if accumulated_state.get("security_block_user"):
                            block_reason = accumulated_state.get("security_block_reason", "Security violation detected")
                            logger.error(f"[SECURITY] Stream ended early - violation detected: {block_reason}")
                            yield _security_event(accumulated_state, block_reason)
                            stream_ended_early = True
                            break

                        if node_name == "detect_malicious_intent":
                            # Security scan completed
                            if accumulated_state.get("security_block_user"):
                                block_reason = accumulated_state.get("security_block_reason", "Malicious intent detected")
                                yield _security_event(accumulated_state, block_reason)
                                stream_ended_early = True
                                break
                        elif node_name == "fix_language":
                            yield {"type": "status", "message": "Normalizing language...", "step": "normalize"}
                        elif node_name == "correct_name_typos":
                            yield {"type": "status", "message": "Checking name corrections...", "step": "name_check"}
                        elif node_name == "classify_intent":
                            intent = node_output.get("intent", "UNKNOWN")
                            yield {"type": "status", "message": f"Intent classified: {intent}", "step": "classify"}
                        elif node_name == "check_schema":
                            yield {"type": "status", "message": "Loading database schema...", "step": "schema"}
                        elif node_name == "retrieve_examples":
                            yield {"type": "status", "message": "Retrieving similar examples...", "step": "rag"}
                        elif node_name == "generate_sql":
                            sql = node_output.get("generated_sql", "")
                            if sql:
                                yield {"type": "sql", "sql": sql[:200] + "..." if len(sql) > 200 else sql, "step": "generate_sql"}
                        elif node_name == "validate_and_fix_sql":
                            yield {"type": "status", "message": "Validating SQL query...", "step": "validate"}
                        elif node_name == "execute_sql":
                            result = node_output.get("query_result", {})
                            if result.get("success"):
                                row_count = result.get("row_count", 0)
                                yield {"type": "status", "message": f"Query executed: {row_count} rows returned", "step": "execute"}
                            else:
                                yield {"type": "error", "message": f"SQL Error: {result.get('error', 'Unknown error')}", "step": "execute"}
                        elif node_name == "story_response":
                            # When story_response node runs, it streams word-by-word via callback
                            # Yield any remaining queued words
                            while word_queue:
                                yield word_queue.pop(0)
                            
                            # Get final response from state
                            final_response = node_output.get("final_response", "") or accumulated_state.get("final_response", "")
                            if final_response:
                                logger.debug(f"[SQL_AGENT] Final response captured in stream ({len(final_response)} chars)")
                        elif node_name == "learn_from_query":
                            yield {"type": "status", "message": "Learning from query...", "step": "learn"}
                    
                    if stream_ended_early:
                        break
            except StopIteration:
                # Stream ended normally
                pass
            except Exception as stream_error:
                logger.error(f"[SQL_AGENT] Stream error: {str(stream_error)}", exc_info=True)
                yield {"type": "error", "message": f"Stream error: {str(stream_error)}", "step": "error"}
                stream_ended_early = True
            
            # Check if stream ended early due to security block
            if stream_ended_early or accumulated_state.get("security_block_user"):
                # Stream ended early - ensure we send completion
                if not final_response:
                    final_response = accumulated_state.get("final_response", _SECURITY_PLACEHOLDER)
                yield {"type": "complete", "message": "Stream ended", "step": "done", "success": False}
                return
            
            # Yield any remaining queued words
            while word_queue:
                yield word_queue.pop(0)
            
            # After stream completes, check accumulated_state for final_response
            if not final_response:
                final_response = accumulated_state.get("final_response", "")
                if final_response:
                    logger.debug(f"[SQL_AGENT] Final response found in accumulated_state ({len(final_response)} chars)")
                    # If we didn't stream word-by-word, stream in chunks as fallback
                    chunk_size = 50
                    for i in range(0, len(final_response), chunk_size):
                        chunk_text = final_response[i:i+chunk_size]
                        yield {"type": "content", "content": chunk_text, "step": "response"}
            
            # Add AI response to conversation memory
            if final_response:
                self.conversation_memory.add_ai_message(final_response)
                logger.info(f"[SQL_AGENT] Streaming query completed successfully (response length: {len(final_response)} chars)")
                # Include the response in completion message for API route to capture
                yield {"type": "complete", "message": "Query completed successfully", "step": "done", "response": final_response, "response_length": len(final_response), "success": True}
            else:
                # Last resort: use invoke to get the final response
                logger.warning("[SQL_AGENT] Final response not found in stream, trying invoke as fallback")
                try:
                    result = self.agent.invoke(initial_state)
                    final_response = result.get("final_response", "")
                    if final_response:
                        self.conversation_memory.add_ai_message(final_response)
                        logger.info(f"[SQL_AGENT] Final response retrieved via invoke ({len(final_response)} chars)")
                        # Stream the complete response
                        chunk_size = 50
                        for i in range(0, len(final_response), chunk_size):
                            chunk_text = final_response[i:i+chunk_size]
                            yield {"type": "content", "content": chunk_text, "step": "response"}
                        yield {"type": "complete", "message": "Query completed successfully", "step": "done", "response_length": len(final_response), "success": True}
                    else:
                        logger.error("[SQL_AGENT] No final_response found even after invoke fallback")
                        yield {"type": "error", "message": "No response generated", "step": "error"}
                        # Always yield completion even on error
                        yield {"type": "complete", "message": "Stream ended with error - no response generated", "step": "done", "success": False}
                except Exception as e:
                    logger.error(f"[SQL_AGENT] Error getting final response via invoke: {str(e)}", exc_info=True)
                    yield {"type": "error", "message": f"No response generated: {str(e)}", "step": "error"}
                
        except Exception as e:
            logger.error(f"[SQL_AGENT] Error in streaming query: {str(e)}", exc_info=True)
            error_msg = f"I encountered an unexpected error: {str(e)}"
            try:
                self.conversation_memory.add_ai_message(error_msg)
            except:
                pass
            yield {"type": "error", "message": error_msg, "step": "error"}
            # Always yield completion to close stream properly
            yield {"type": "complete", "message": "Stream ended with error", "step": "done", "success": False}

    def query_with_details(self, user_input: str, learn: bool = False) -> dict:
        """
        Process a query and return full details (for debugging).

        Args:
            user_input: The natural language query from the user
            learn: Whether to save successful queries for future reference

        Returns:
            Complete state dictionary with all intermediate results
        """
        initial_state = self._create_initial_state(user_input, should_learn=learn)

        try:
            return self.agent.invoke(initial_state)
        except Exception as e:
            initial_state["error"] = str(e)
            return initial_state

    def test_connection(self) -> bool:
        """Test the database connection."""
        try:
            logger.debug("[SQL_AGENT] Testing database connection")
            conn = self.db.get_connection()
            conn.close()
            logger.info("[SQL_AGENT] Database connection test successful")
            return True
        except Exception as e:
            logger.error(f"[SQL_AGENT] Database connection test failed: {str(e)}")
            return False

    def get_schema(self) -> str:
        """Get the database schema description."""
        return self.db.get_schema_description()

    def get_knowledge_base_stats(self) -> Dict:
        """Get statistics about the knowledge base."""
        return self.kb.get_stats()
