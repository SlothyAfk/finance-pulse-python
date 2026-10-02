"""Finance Pulse: financial news as structured, sourced statements (https://fintopic.news/docs)."""
from .client import RAPIDAPI, Backend, FinancePulse, FinancePulseError

__all__ = ["FinancePulse", "FinancePulseError", "Backend", "RAPIDAPI"]
__version__ = "0.1.0"
