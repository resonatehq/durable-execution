# Examples

Applications, not engine. Each is a directory a user could have written:
a `main.py` whose only wiring is one import, a `requirements.txt`, and
whatever else that application needs. Nothing in them reaches past the
package's published surface, which `test_deploy.py` and
`test_example_agent.py` both check.

| | |
|---|---|
| [`research-agent/`](research-agent/) | the program from the repository README: plan, fan out, synthesise. What every test in the project drives |
| [`travel-agent/`](travel-agent/) | a translation of Temporal's durable-AI-agent tutorial: a conversation, tools, and a person confirming the step that spends money |
