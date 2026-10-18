# Execution-site policy

`Core(..., execution_policy=ExecutionPolicy(...))` gates generation, classification
and tool invocation before dispatch. An omitted policy retains the legacy library
behavior; service entry points must register an explicit policy and must not let
request bodies replace the administrator's backend or tool-location registry.

```python
from cloudyfan_jev.privacy import ExecutionPolicy

policy = ExecutionPolicy(
    mode='local_only',
    origin='home:household-1',
    classifier='cloud:typesafe',
    tools={'read_workspace': 'home:household-1'},
)
```

Locations use `home:<site>`, `company:<site>` or `cloud:<site>`. The task origin,
each selected model profile's `execution_location`, classifier and invoked tools
must have known locations. `local_only` permits only the exact origin site;
company infrastructure is not a household, even when the site suffix matches.
Unknown locations are denied under both explicit modes. `unrestricted` permits
known remote locations, but does not authorize payment or side effects.

The current Jev Decisions adapter is a cloud service and cannot be labeled local.
A generation profile cannot become local by relabeling an OpenRouter profile:
it must use a registered local backend with the matching execution site. A local
first answer that fails validation stops before a cloud classifier or cloud
repair. No alternate site is selected automatically. A public pass skips Jev.

The full policy is frozen in the run manifest. Adding, removing or changing it
requires a new run identifier. A stop raises `PolicyDenied`, a `RoutingError`
with HTTP status 403 and `location_policy_denied`. Its public body contains only
stage and reason, never task text, answers, tool arguments or site identifiers.
Existing receipts remain available for accounting after a policy stop.

These are dispatch checks on trusted, server-registered adapters, not an operating
system network sandbox. Tool declarations must describe all execution and egress;
a local tool that secretly sends data remotely violates its registration. The
embedding application must sandbox tools and validators and control adapters,
proxies and credentials. Offline tests block real sockets and monitor injected
HTTP boundaries. They do not prove a real local engine or production network is
isolated. Payment authorization and validation correctness remain separate gates.
