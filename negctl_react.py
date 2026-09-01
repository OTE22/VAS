"""Break each ReAct property; the contract suite MUST catch it.

A contract suite that passes against a broken architecture is decoration.
"""
import io, subprocess, sys

MUTATIONS = [
    ("a decider runs after the loop again",
     "sql_agent/tools/agent_tools.py",
     "    def plan_action(self, state: AgentState) -> AgentState:",
     "    def classify_intent(self, state):\n        return state\n\n    def plan_action(self, state: AgentState) -> AgentState:",
     "tests/test_react_contract.py -k 'no_second_decider'"),

    ("document actions end the turn unobserved",
     "sql_agent/graph.py",
     '            {"done": END, "observe": "observe_and_replan"},',
     '            {"done": END, "observe": END},',
     "tests/test_react_contract.py -k 'terminal_action_is_observed'"),

    ("the AST guard is bypassed",
     "sql_agent/graph.py",
     '    workflow.add_edge("prepare_sql_for_execution", "execute_sql")',
     '    workflow.add_edge("prepare_sql_for_execution", "execute_sql")\n    workflow.add_edge("retrieve_examples", "execute_sql")',
     "tests/test_react_contract.py -k 'ast_guard'"),

    ("the loop stops validating tool calls",
     "sql_agent/tools/agent_loop.py",
     "            arguments = tr.validate_call(name, call.get(\"arguments\"))",
     "            arguments = call.get(\"arguments\") or {}",
     "tests/test_react_contract.py -k 'python_disposes'"),

    ("the intent-fit gate is removed",
     "sql_agent/tools/agent_loop.py",
     "                is_a_request = asked_for_an_action(llm, user_text)",
     "                is_a_request = True",
     "tests/test_react_contract.py -k 'user_asked_for_something'"),

    ("re-planning becomes unbounded",
     "sql_agent/graph.py",
     "            max_replans=int(settings.SQL_AGENT_MAX_REPLANS),",
     "            max_replans=99,",
     "tests/test_react_contract.py -k 'termination_is_arithmetic'"),
]

failures = []
for label, path, old, new, target in MUTATIONS:
    src = io.open(path, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in src else "\n"
    needle = old.replace("\n", nl)
    if src.count(needle) < 1:
        print(f"SKIP (anchor 0x): {label}")
        failures.append(label); continue
    try:
        io.open(path, "w", encoding="utf-8", newline="").write(
            src.replace(needle, new.replace("\n", nl), 1))
        rc = subprocess.run(f"python -m pytest {target} -q --no-header -p no:randomly",
                            shell=True, capture_output=True, text=True)
    finally:
        io.open(path, "w", encoding="utf-8", newline="").write(src)
    caught = rc.returncode != 0
    print(f"{'CAUGHT ' if caught else 'MISSED '} {label}")
    if not caught: failures.append(label)

print()
print("every ReAct property is defended" if not failures
      else f"NOT DEFENDED: {failures}")
sys.exit(1 if failures else 0)
