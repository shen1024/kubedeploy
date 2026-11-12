"""异常类型。"""

from __future__ import annotations


class KubeDeployError(Exception):
    """可展示给用户的错误。"""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class ValidationError(KubeDeployError):
    pass


class TransportError(KubeDeployError):
    pass


class AgentError(KubeDeployError):
    def __init__(self, message, action=None, details=None, hint=None):
        super().__init__(message, hint=hint)
        self.action = action
        self.details = details or []


class PreflightError(KubeDeployError):
    pass


class StateError(KubeDeployError):
    pass


class UserAbort(KubeDeployError):
    pass
