"""The one kind of error whose message is for the person.

Raise UserError for something they can fix ("That transaction no longer exists.", "Pick another
account to merge into."): service.BudgetService.dispatch() shows its text on the page as it is.
Anything else that goes wrong is a bug: it's logged with its traceback and shown as "Something went
wrong (…). Details are in the log." so the log (see the Debugging section of CLAUDE.md) has it.
"""


class UserError(Exception):
    """Something the person can fix; the message is shown on the page."""
