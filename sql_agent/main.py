"""
Main Entry Point
=================
CLI entry point for the SQL Intelligence Agent.
"""

import json
import sys
import os

# Add parent directory to path to support both direct execution and module execution
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Try relative imports first (when run as module), fall back to absolute imports (when run directly)
try:
    from .config import config
    from .agent import SQLIntelligenceAgent
    from .conversation_memory import ConversationMemory
except ImportError:
    # Fall back to absolute imports when running directly
    from sql_agent.config import config
    from sql_agent.agent import SQLIntelligenceAgent
    from sql_agent.conversation_memory import ConversationMemory


def main():
    """Main entry point for the SQL Intelligence Agent."""
    print("=" * 60)
    print("🤖 SQL Intelligence Agent")
    print("=" * 60)
    print(f"Model: {config.ollama_model}")
    print(f"Database: {config.db_name}@{config.db_host}:{config.db_port}")
    print("=" * 60)

    # Initialize conversation memory
    memory = ConversationMemory()
    session_id = memory.start_session()
    print(f"💾 Conversation session: {session_id}")
    print("=" * 60)

    # Create agent with conversation memory
    agent = SQLIntelligenceAgent(conversation_memory=memory)

    # Test database connection
    print("\n🔌 Testing database connection...")
    if agent.test_connection():
        print("✅ Database connection successful!")
    else:
        print("❌ Database connection failed!")
        print("Please check your database configuration.")
        return

    # Show schema
    print("\n📋 Database Schema:")
    print(agent.get_schema())

    # Interactive loop
    print("\n" + "=" * 60)
    print("Enter your questions (type 'quit' to exit):")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n👤 You: ").strip()

            if not user_input:
                continue

            if user_input.lower() in ['quit', 'exit', 'q']:
                print("\n👋 Goodbye!")
                break

            if user_input.lower() == 'schema':
                print(agent.get_schema())
                continue

            if user_input.lower() == 'stats':
                print("\n📊 Knowledge Base Statistics:")
                print(json.dumps(agent.get_knowledge_base_stats(), indent=2))
                continue

            if user_input.lower() == 'debug':
                # Debug mode - show all intermediate state
                debug_input = input("Enter query for debug: ").strip()
                result = agent.query_with_details(debug_input)
                print("\n🔍 Debug Output:")
                print(json.dumps({k: v for k, v in result.items() if k != 'messages'}, indent=2, default=str))
                continue

            # Process the query
            print("\n🤖 Agent: ", end="")
            response = agent.query(user_input)
            print(response)

        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Error: {str(e)}")


if __name__ == "__main__":
    main()
