class ExecutionUnavailable(RuntimeError):
    """Fatal execution failure. No retry on another backend is permitted."""
