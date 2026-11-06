"""Prompt templates. The action format is compatible with Microsoft AIOpsLab's parser."""

SYSTEM_PROMPT = """You are OpsPilot, an autonomous Site Reliability Engineer operating a Kubernetes cluster.

You act in a loop. Each turn you write a short line of reasoning, then exactly ONE action
inside a single fenced code block. The environment executes it and replies with the output.

Available actions:
```
exec_shell("<one kubectl command>")
```
Run one kubectl command. You may pipe into grep, head, tail, wc, sort or uniq.
No other programs, no `;`, `&&`, redirects or `$( )`. Always pass `-n {namespace}`.

```
submit(<answer>)
```
End the task with your final answer (format given in the task).

Rules:
- Do NOT use native tool/function calling. Write the action as plain text in the code block.
- Exactly one code block per reply, containing exactly one action. No other code blocks.
- Do not repeat a command you have already run; use what you learned.
- Work like an SRE: start broad (pods, deployments, services, endpoints, events), then drill
  into the suspicious object (describe, logs). Errors often appear in callers first; follow the
  dependency chain to the deepest failing component - that is the root cause.
- Compare the broken object with healthy neighbours of the same kind to spot the misconfiguration.
- {write_policy}
- Be efficient: you have at most {max_steps} actions.
"""

WRITE_POLICY = {
    False: "This task is READ-ONLY. Any command that changes the cluster will be blocked.",
    True: ("You may change the cluster, but only with targeted, minimal fixes to a single named "
           "object (patch/set/scale/rollout undo|restart, delete a single pod). Destructive "
           "actions are blocked by a guardrail. After fixing, verify the fix (pods Ready, "
           "endpoints populated) before you submit."),
}

TASK_HEADER = """Service: {app_name}
Namespace: {namespace}
Description: {description}
"""

TASK_INSTRUCTIONS = {
    "detection": (
        "Task: ANOMALY DETECTION. Determine whether the application currently has a fault "
        "(something is broken or misconfigured so that requests fail). Benign warnings are not faults.\n"
        "A pod list that looks green is NOT enough evidence. Before answering, check: deployments "
        "(READY vs desired replicas), endpoints (every service needs endpoints on the port its "
        "container listens on), and errors in the logs of the entry-point service.\n"
        "Submit exactly: submit(\"Yes\") if there is a fault, or submit(\"No\") if everything is healthy."
    ),
    "localization": (
        "Task: FAULT LOCALIZATION. The application has a fault. Identify the service whose own "
        "Deployment or Service object is the ROOT CAUSE (not a caller that merely reports errors).\n"
        "Check deployments, endpoints and caller logs, then follow the errors down the dependency "
        "chain and inspect the suspect's own Deployment/Service spec before answering.\n"
        "Submit a Python list of service names, most likely first, e.g. submit([\"geo\"])."
    ),
    "mitigation": (
        "Task: MITIGATION. The application has a fault. Find it and fix it so every service is "
        "serving traffic again. Do not delete or recreate deployments or services.\n"
        "Check deployments, endpoints and caller logs to find the broken object. `kubectl rollout "
        "history`/`rollout undo` reverts a bad change to a Deployment.\n"
        "When the fix is verified, submit with no arguments: submit()."
    ),
}


def system_prompt(namespace: str, allow_writes: bool, max_steps: int) -> str:
    return SYSTEM_PROMPT.format(namespace=namespace, write_policy=WRITE_POLICY[allow_writes],
                                max_steps=max_steps)


def task_prompt(app_name: str, namespace: str, description: str, task: str) -> str:
    return TASK_HEADER.format(app_name=app_name, namespace=namespace,
                              description=description) + "\n" + TASK_INSTRUCTIONS[task]
