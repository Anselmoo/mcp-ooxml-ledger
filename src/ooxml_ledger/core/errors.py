"""Exception hierarchy. Every failure this package raises derives from OoxmlLedgerError."""


class OoxmlLedgerError(Exception):
    """Base for every error raised by this package."""


class PackageError(OoxmlLedgerError):
    """A package could not be safely opened or written."""


class XmlSecurityError(OoxmlLedgerError):
    """A part carried markup that is unsafe to parse, such as a DOCTYPE."""


class VerificationError(OoxmlLedgerError):
    """A receipt could not be checked — malformed, or an unsupported version."""


class EditRefused(OoxmlLedgerError):
    """A guard refused an edit. The message always says which guard and why.

    A refusal is a success of the design, not a failure of it: design §4.3 requires that
    constructs outside the revision vocabulary be refused rather than silently mishandled.
    """


class EditNotFound(OoxmlLedgerError):
    """The requested phrase does not exist in the addressed content."""


class GateFailure(OoxmlLedgerError):
    """The commit gate refused a write. Carries the divergences it found."""

    def __init__(self, message: str, failures: list[str] | None = None) -> None:
        super().__init__(message)
        self.failures = failures or []
