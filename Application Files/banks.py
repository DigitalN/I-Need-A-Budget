"""Bank names: one spelling per bank, whatever a file calls it.

The bank on an account is free text (there are hundreds of credit unions); `SUGGESTED`
only feeds the suggestions shown while typing it. `canonical()` turns the names files
use ("Royal Bank of Canada", "RBC Royal Bank") into the one shown ("RBC"), and `slug()`
is what account keys use, so an RBC account is the same account whether it came from a
CSV or a Quicken file.

Public institution names only. Never the person's own data.
"""

import re

# Shown name → other names files use for it.
_NAMES = {
    "Tangerine": ["ING Direct", "ING Bank of Canada", "Tangerine Bank"],
    "RBC": ["Royal Bank", "Royal Bank of Canada", "RBC Royal Bank", "RBC Banque Royale"],
    "TD": ["TD Canada Trust", "Toronto-Dominion Bank", "TD Bank"],
    "BMO": ["Bank of Montreal", "BMO Bank of Montreal", "BMO Banque de Montréal"],
    "Scotiabank": ["Bank of Nova Scotia", "The Bank of Nova Scotia", "Scotia"],
    "CIBC": ["Canadian Imperial Bank of Commerce"],
    "National Bank": ["National Bank of Canada", "Banque Nationale", "NBC"],
    "ATB": ["ATB Financial", "Alberta Treasury Branches"],
    "Desjardins": ["Caisse Desjardins", "Mouvement Desjardins"],
    "Simplii Financial": ["Simplii"],
    "EQ Bank": ["Equitable Bank"],
    "PC Financial": ["President's Choice Financial", "PC Money"],
    "Laurentian Bank": ["Banque Laurentienne"],
    "HSBC": ["HSBC Bank Canada"],
    "American Express": ["Amex"],
    "Capital One": [],
    "MBNA": [],
    "Rogers Bank": [],
    "Neo Financial": ["Neo"],
    "KOHO": [],
    "Wealthsimple": [],
    "Brim Financial": ["Brim"],
    "Motusbank": ["motus bank"],
    "Manulife Bank": [],
    "Home Trust": [],
    "Canadian Western Bank": ["CWB"],
    "Servus Credit Union": ["Servus"],
    "Connect First Credit Union": ["Connect First"],
    "Vancity": ["Vancouver City Savings Credit Union"],
    "Coast Capital Savings": ["Coast Capital"],
    "First West Credit Union": ["First West"],
    "Prospera Credit Union": ["Prospera"],
    "Meridian Credit Union": ["Meridian"],
    "Alterna Savings": ["Alterna"],
    "DUCA Credit Union": ["DUCA"],
    "Libro Credit Union": ["Libro"],
    "Affinity Credit Union": ["Affinity"],
    "Conexus Credit Union": ["Conexus"],
    "Steinbach Credit Union": [],
    "Cambrian Credit Union": ["Cambrian"],
    "Assiniboine Credit Union": [],
    "UNI Financial": ["UNI Coopération financière"],
}
SUGGESTED = sorted(_NAMES, key=str.lower)


def slug(name):
    """'Royal Bank of Canada' → 'rbc': lower-case letters and digits of the canonical name."""
    return re.sub(r"[^a-z0-9]", "", canonical(name).lower())


def _key(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


_ALIASES = {_key(alias): shown for shown, aliases in _NAMES.items() for alias in [shown, *aliases]}


def canonical(name):
    """The name to show for a bank a file names, or the name as it was given."""
    name = " ".join(str(name or "").split())
    return _ALIASES.get(_key(name), name)
