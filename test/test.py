# test.py
from datetime import date
from src.utils import is_trading_day, is_special_session

# Test Feb 1, 2025 (Saturday - Budget Day)
feb1 = date(2026, 2, 1)
print(f"Feb 1, 2025 is special session: {is_special_session(feb1)}")
print(f"Feb 1, 2025 is trading day: {is_trading_day(feb1)}")

# Test Feb 2, 2025 (Sunday - Regular holiday)
feb2 = date(2025, 2, 2)
print(f"Feb 2, 2025 is special session: {is_special_session(feb2)}")
print(f"Feb 2, 2025 is trading day: {is_trading_day(feb2)}")