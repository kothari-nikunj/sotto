"""One stage receipt protocol; the pipeline owns its lock, binding and authority guard."""


class CheckpointStages:
    def __init__(self, checkpoint, save, guard, held_message):
        self.checkpoint = checkpoint
        self.save = save
        self.guard = guard
        self.held_message = held_message

    def __call__(self, name, invoke, *, retry_completed=False):
        self.guard()
        saved = self.checkpoint.get(name)
        if saved is not None:
            if saved.get('status') != 'completed':
                raise RuntimeError(self.held_message)
            if not retry_completed:
                return saved['result']
        self.checkpoint[name] = {'status': 'started'}
        self.save()
        result = invoke()
        self.checkpoint[name] = {'status': 'completed', 'result': result}
        self.save()
        return result
