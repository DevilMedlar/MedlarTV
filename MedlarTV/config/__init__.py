"""
MedlarTV Configuration Module
YAML configuration files for personality, moods, commands, etc.
"""

import yaml
import os
from pathlib import Path

def load_config(config_name: str) -> dict:
    """
    Load a configuration file from the config directory.
    
    Args:
        config_name: Name of config file (e.g., 'personality', 'moods')
    
    Returns:
        Dict containing configuration data
    """
    config_dir = Path(__file__).parent
    config_path = config_dir / f"{config_name}.yaml"
    
    if not config_path.exists():
        return {}
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

__all__ = ['load_config']