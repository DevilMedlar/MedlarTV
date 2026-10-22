"""
MedlarTV Core Module
Contains the brain, memory, sentiment analysis, context tracking, emotional system, and all features
"""

# Memory System
from .memory import (
    load_memory,
    save_memory,
    record_mood,
    get_dominant_mood,
    get_dominant_weighted_mood,
    reset_memory_on_shutdown
)

# LLM Brain
from .llm_brain import (
    generate_response,
    check_ollama_health,
    clear_history
)

# Context Tracking & Expression (from mood_system)
from .mood_system import (
    compute_mood,
    get_mood_label
)

# Sentiment Analysis (using sentiment_advanced as replacement for deleted sentiment.py)
from .sentiment_advanced import analyze_sentiment_simple as analyze_sentiment

# Fuzzy Trigger Detection
from .fuzzy_trigger import (
    should_respond as fuzzy_should_respond,
    find_triggers_in_message,
    is_trigger_word
)

# Translation Module
from .translation import (
    detect_language,
    get_multilingual_greeting,
    add_language_indicator,
)

# Interaction Logger Module
from .interaction_logger import (
    log_interaction,
    log_command,
    log_mood_change,
    log_error,
    get_interaction_stats
)

# Moderation Module
from .moderation import (
    check_message,
    execute_timeout,
    execute_ban,
    execute_delete,
    handle_mod_command,
    is_mod_command,
    load_link_whitelist,
    get_user_timeout_count
)

# Stream Management Module
from .stream_management import (
    get_stream_info,
    get_channel_info,
    update_stream_title,
    update_stream_category,
    search_game,
    get_top_games,
    format_stream_info
)

# Twitch Events Module
from .twitch_events import (
    detect_raid,
    detect_subscription,
    detect_channel_point_redemption,
    detect_bits,
    get_raid_response,
    get_sub_response,
    get_channel_point_response,
    get_bits_response,
    load_global_emotes,
    load_channel_emotes,
    add_random_emote,
    extract_emotes_from_message
)

# Content Filter Module
from .content_filter import (
    filter_message,
    should_enable_all_caps,
    get_safety_response
)

# ⭐ NEW: Advanced Emotional System
try:
    from .emotional_system import (
        get_emotional_system,
        process_chat_emotion,
        get_current_emotion,
        get_emotion_state,
        boost_emotion,
        reset_emotions
    )
    
    from .sentiment_advanced import (
        analyze_sentiment_advanced,
        analyze_sentiment_simple,
        detect_emotional_keywords,
        get_sentiment_description
    )
    
    from .emotion_emote_selector import (
        get_emotion_emote,
        add_emotion_emote,
        format_message_with_emotions,
        get_multiple_emotion_emotes
    )
    
    EMOTIONAL_SYSTEM_AVAILABLE = True
except ImportError:
    # Emotional system not yet installed
    EMOTIONAL_SYSTEM_AVAILABLE = False

__all__ = [
    # Memory functions
    'load_memory',
    'save_memory',
    'record_mood',
    'get_dominant_mood',
    'get_dominant_weighted_mood',
    'reset_memory_on_shutdown',
    
    # LLM Brain functions
    'generate_response',
    'check_ollama_health',
    'clear_history',
    
    # Context functions
    'compute_mood',
    'get_mood_label',
    
    # Expression & Sentiment (Legacy)
    'analyze_sentiment',
    
    # Fuzzy Trigger Detection
    'fuzzy_should_respond',
    'find_triggers_in_message',
    'is_trigger_word',
    
    # Interaction Logging
    'log_interaction',
    'log_command',
    'log_mood_change',
    'log_error',
    'get_interaction_stats',
    
    # Moderation
    'check_message',
    'execute_timeout',
    'execute_ban',
    'execute_delete',
    'handle_mod_command',
    'is_mod_command',
    'load_link_whitelist',
    'get_user_timeout_count',
    
    # Stream Management
    'get_stream_info',
    'get_channel_info',
    'update_stream_title',
    'update_stream_category',
    'search_game',
    'get_top_games',
    'format_stream_info',
    
    # Twitch Events
    'detect_raid',
    'detect_subscription',
    'detect_channel_point_redemption',
    'detect_bits',
    'get_raid_response',
    'get_sub_response',
    'get_channel_point_response',
    'get_bits_response',
    'load_global_emotes',
    'load_channel_emotes',
    'add_random_emote',
    'extract_emotes_from_message',
    
    # Content Filter
    'filter_message',
    'should_enable_all_caps',
    'get_safety_response',
]

# Add emotional system exports if available
if EMOTIONAL_SYSTEM_AVAILABLE:
    __all__.extend([
        # Advanced Emotional System
        'get_emotional_system',
        'process_chat_emotion',
        'get_current_emotion',
        'get_emotion_state',
        'boost_emotion',
        'reset_emotions',
        
        # Advanced Sentiment
        'analyze_sentiment_advanced',
        'analyze_sentiment_simple',
        'detect_emotional_keywords',
        'get_sentiment_description',
        
        # Emotion-Aware Emotes
        'get_emotion_emote',
        'add_emotion_emote',
        'format_message_with_emotions',
        'get_multiple_emotion_emotes',
        
        # Status flag
        'EMOTIONAL_SYSTEM_AVAILABLE',
    ])