# Examples

Applications, not engine. Each is a directory a user could have written:
a `main.py` whose only wiring is `handler = serve()` on its last line, a
`requirements.txt`, and whatever else that application needs. Nothing in
them reaches past the package's published surface, which `test_userapp.py`
and `test_example_agent.py` check; `test_deploy.py` checks each one would
build.

| | |
|---|---|
| [`research-agent/`](research-agent/) | the program from the repository README: plan, fan out, synthesise. What `test_http.py` serves |
| [`travel-agent/`](travel-agent/) | a translation of Temporal's durable-AI-agent tutorial: a conversation, tools, and a person confirming the step that spends money |
