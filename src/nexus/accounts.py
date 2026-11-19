"""Account identities and shared Codex quota snapshots."""

import asyncio
import time

from .errors import NexusError
from .events import now


class AccountLimits:
    def __init__(self, service):
        self.service = service
        self.identities = {}
        self.usage = {}
        self.cached = None
        self.expires = 0
        self.revision = 0
        self.lock = asyncio.Lock()

    def invalidate(self, machine_id=None, *, identity=False):
        self.expires = 0
        self.revision += 1
        if identity:
            self.identities.pop(machine_id, None)

    async def _identity(self, machine):
        machine_id = machine["id"]
        if machine["status"] != "connected":
            return self.identities.get(machine_id, {}), None
        try:
            result = await self.service.rpc(machine_id, "account/read", {"refreshToken": False})
            account = result.get("account") or {}
            auth_type = account.get("type") or "loggedOut"
            provider_name = None
            if result.get("requiresOpenaiAuth") is False and auth_type not in {
                "amazonBedrock",
                "gcp",
            }:
                auth_type = "external"
                config = (await self.service.rpc(machine_id, "config/read", {}))["config"]
                provider = config.get("model_provider") or "openai"
                provider_name = (config.get("model_providers") or {}).get(provider, {}).get("name")
                provider_name = provider_name or provider
            identity = {
                "authType": auth_type,
                "providerName": provider_name,
                "userId": result.get("userId"),
                "accountId": (result.get("workspaceRouting") or {}).get("chatgptAccountId"),
                "email": account.get("email"),
                "planType": account.get("planType"),
                "workspaceName": account.get("workspaceName"),
            }
            self.identities[machine_id] = identity
            return identity, None
        except NexusError as error:
            return self.identities.get(machine_id, {}), str(error)

    async def _limits(self, account):
        previous = self.usage.get(account["id"], {})
        account.update(usage=previous.get("usage"), updatedAt=previous.get("updatedAt"))
        account["error"] = next(
            (machine["error"] for machine in account["machines"] if machine.get("error")),
            "机器未连接",
        )
        for machine in account["machines"]:
            if machine["status"] != "connected" or machine.get("error"):
                continue
            try:
                result = await self.service.rpc(machine["id"], "account/rateLimits/read", {})
                if (
                    account["accountId"]
                    and result.get("accountId")
                    and account["accountId"] != result["accountId"]
                ):
                    self.invalidate(machine["id"], identity=True)
                    raise NexusError("账号已切换，请刷新")
                account.update(usage=result, updatedAt=now(), error=None)
                self.usage[account["id"]] = {
                    "usage": account["usage"],
                    "updatedAt": account["updatedAt"],
                }
                return
            except NexusError as error:
                account["error"] = str(error)

    async def read(self, *, refresh=False):
        async with self.lock:
            if not refresh and self.cached is not None and time.monotonic() < self.expires:
                return self.cached
            revision = self.revision
            machines = await self.service.machines()
            identities = await asyncio.gather(*(self._identity(machine) for machine in machines))
            accounts, others = {}, []
            for machine, (identity, error) in zip(machines, identities):
                member = {key: machine[key] for key in ("id", "name", "status")}
                member["error"] = error
                if identity.get("authType") != "chatgpt":
                    others.append(
                        {**member, **identity, "authType": identity.get("authType", "unavailable")}
                    )
                    continue
                # Missing IDs keep a machine separate until Codex supplies its identity.
                key = f"{identity['userId'] or machine['id']}:{identity['accountId'] or machine['id']}"
                if key not in accounts:
                    accounts[key] = {**identity, "id": key, "machines": []}
                accounts[key]["machines"].append(member)
            await asyncio.gather(*(self._limits(account) for account in accounts.values()))
            self.cached = {"accounts": list(accounts.values()), "otherMachines": others}
            self.expires = time.monotonic() + 30 if revision == self.revision else 0
            return self.cached
