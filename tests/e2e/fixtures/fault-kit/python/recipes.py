def count_recipes(recipes):
    """How many recipes have a name."""
    return len([r for r in recipes if str(r or "").strip()])
