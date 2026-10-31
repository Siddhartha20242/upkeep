"""A tiny project pinned to PyYAML 5.4.1. `yaml.load(text)` stops working in PyYAML 6 because Loader became required."""
import yaml


def read_config(text):
    return yaml.load(text)


def read_safely(text):
    return yaml.load(text, Loader=yaml.SafeLoader)   # already fine: not flagged
