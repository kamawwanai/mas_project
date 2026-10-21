from .common import session, public_task


class SingleAgent:
    def run(self, client, registry):
        return session(client, registry, "single_agent", public_task(registry))
