from pricing import calculate_discount


def discounted_total(price: float, rate: float) -> float:
    return calculate_discount(price, rate)
