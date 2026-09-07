from src.counts import parse_count

assert parse_count("") == 0
assert parse_count("7") == 7
