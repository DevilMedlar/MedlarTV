"""
MedlarTV Enhanced Search Integration
- DuckDuckGo for current web info (news, prices, events)
- Wikipedia for factual/encyclopedic information
Both 100% FREE, no API keys required
"""

import os
import re
from typing import List, Dict, Optional

# Importing broad Core must not build optional backends or read dotenv files.
DEBUG = False
ENABLE_WEB_SEARCH = True
ENABLE_WIKIPEDIA = True
WEB_SEARCH_MAX_RESULTS = 3
WIKIPEDIA_LANG = "en"
DDGS = None
wiki = None
_search_configured = False


def _positive_result_limit(value, default=3):
    try:
        if isinstance(value, bool):
            raise ValueError("Boolean limit")
        limit = int(value)
        if limit <= 0:
            raise ValueError("Nonpositive limit")
        return limit
    except (ValueError, TypeError, OverflowError):
        print("[Web Search] Invalid result limit; using default.")
        return default


def _configure_search():
    global DEBUG, ENABLE_WEB_SEARCH, ENABLE_WIKIPEDIA, WEB_SEARCH_MAX_RESULTS, WIKIPEDIA_LANG, _search_configured
    if _search_configured:
        return
    # Preserve standalone command dotenv configuration, but defer it to an
    # explicit search rather than package startup. Never print its values.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except (ImportError, OSError):
        print("[Web Search] Optional dotenv configuration unavailable.")
    DEBUG = os.getenv("MEDLARTV_DEBUG", "false").lower() == "true"
    ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "true").lower() == "true"
    ENABLE_WIKIPEDIA = os.getenv("ENABLE_WIKIPEDIA", "true").lower() == "true"
    WEB_SEARCH_MAX_RESULTS = _positive_result_limit(os.getenv("WEB_SEARCH_MAX_RESULTS", "3"))
    WIKIPEDIA_LANG = os.getenv("WIKIPEDIA_LANG", "en")
    _search_configured = True


def _get_ddgs():
    global DDGS
    if DDGS is None:
        from ddgs import DDGS as backend
        DDGS = backend
    return DDGS()


def search_web(query: str, max_results: int = None) -> List[Dict]:
    """
    Search the web using DuckDuckGo (completely free, no API key).
    Best for: current events, news, prices, recent information
    
    Args:
        query: Search query string
        max_results: Maximum number of results (default: from env or 3)
    
    Returns:
        List of search results with title, body, and href
    """
    _configure_search()
    if DEBUG:
        print(f"[DEBUG enhanced_search] search_web() query_length={len(query)}, max_results={max_results!r}")

    if not ENABLE_WEB_SEARCH:
        if DEBUG:
            print("[DEBUG enhanced_search] search_web() → web search disabled, returning []")
            print("[Web Search] Disabled in configuration")
        return []
    
    if max_results is None:
        max_results = WEB_SEARCH_MAX_RESULTS
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_web() max_results was None, using default={max_results}")

    max_results = _positive_result_limit(max_results, WEB_SEARCH_MAX_RESULTS)
    try:
        if DEBUG:
            print("[DEBUG enhanced_search] search_web() creating DDGS() context")
        results = list(_get_ddgs().text(query, max_results=max_results, backend="auto"))
        if results:
            if DEBUG:
                print(f"[DEBUG enhanced_search] search_web() ddgs.text() returned {len(results)} results")
            print(f"[Web Search] Found {len(results)} results")
            return results
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_web() exception_type={type(e).__name__}")
        print(f"[Web Search] Error: {type(e).__name__}")
    return []


def search_wikipedia(query: str, sentences: int = 3) -> Optional[Dict]:
    """
    Search Wikipedia for factual/encyclopedic information.
    Best for: definitions, historical facts, general knowledge
    
    Args:
        query: Search term
        sentences: Number of sentences to include in summary (default: 3)
    
    Returns:
        Dict with title, summary, and url, or None if not found
    """
    global wiki
    _configure_search()
    if DEBUG:
        print(f"[DEBUG enhanced_search] search_wikipedia() query_length={len(query)}, sentences={sentences}")

    if not ENABLE_WIKIPEDIA:
        if DEBUG:
            print("[DEBUG enhanced_search] search_wikipedia() → wikipedia disabled, returning None")
        print("[Wikipedia] Disabled in configuration")
        return None
    
    try:
        # Clean up query for Wikipedia
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_wikipedia() original query_length={len(query)}")
        clean_query = query.replace("what is ", "").replace("who is ", "").replace("?", "").strip()
        clean_query = clean_query.title() if not clean_query.isupper() else clean_query
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_wikipedia() clean_query_length={len(clean_query)}")
        
        # Try to get the page
        if DEBUG:
            print("[DEBUG enhanced_search] search_wikipedia() calling wiki.page()")
        if wiki is None:
            import wikipediaapi
            wiki = wikipediaapi.Wikipedia(language=WIKIPEDIA_LANG,
                user_agent='MedlarTV/1.0 (Twitch Bot; Python/wikipediaapi)')
        page = wiki.page(clean_query)
        
        if not page.exists():
            if DEBUG:
                print("[DEBUG enhanced_search] search_wikipedia() page.exists() is False")
            print("[Wikipedia] Page not found")
            return None
        
        if DEBUG:
            print("[DEBUG enhanced_search] search_wikipedia() page.exists() is True")
        # Get summary (first N sentences)
        summary = page.summary
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_wikipedia() full summary length={len(summary)}")
        sentences_list = summary.split('. ')[:sentences]
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_wikipedia() sentences_list_len={len(sentences_list)}")
        short_summary = '. '.join(sentences_list) + ('.' if not sentences_list[-1].endswith('.') else '')
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_wikipedia() short_summary length={len(short_summary)}")
        
        result = {
            "title": page.title,
            "summary": short_summary,
            "url": page.fullurl,
            "exists": True
        }
        
        if DEBUG:
            print("[DEBUG enhanced_search] search_wikipedia() returning result")
        print("[Wikipedia] Found a page")
        return result
        
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_wikipedia() exception_type={type(e).__name__}")
        print(f"[Wikipedia] Error: {type(e).__name__}")
        return None


def should_search_web(message: str) -> bool:
    """
    Determine if message needs WEB search (current/time-sensitive info).
    
    Returns True for:
    - Price queries (bitcoin price, cost of, how much)
    - Current events (today, latest, recent)
    - News (news about, update on)
    - Time references (2024, 2025, this year)
    """
    if DEBUG:
        print(f"[DEBUG enhanced_search] should_search_web() message_length={len(message)}")

    if not ENABLE_WEB_SEARCH:
        if DEBUG:
            print("[DEBUG enhanced_search] should_search_web() → web search disabled, returning False")
        return False
    
    message_lower = message.lower()
    if DEBUG:
        print(f"[DEBUG enhanced_search] should_search_web() normalized_length={len(message_lower)}")
    
    # Current/time-sensitive triggers
    web_triggers = [
        "price of", "cost of", "how much",
        "latest", "recent", "current", "today", "now",
        "news about", "update on",
        "2024", "2025", "this year", "this month",
        "stock price", "crypto", "bitcoin", "ethereum",
    ]
    
    match = any(trigger in message_lower for trigger in web_triggers)
    if DEBUG:
        print(f"[DEBUG enhanced_search] should_search_web() matched_triggers={match}")
    return match


def should_search_wikipedia(message: str) -> bool:
    """
    Determine if message needs WIKIPEDIA search (factual/encyclopedic info).
    
    Returns True for:
    - Definition queries (what is, who is, define)
    - Historical queries (history of, who was)
    - General knowledge (tell me about, explain)
    """
    if DEBUG:
        print(f"[DEBUG enhanced_search] should_search_wikipedia() message_length={len(message)}")

    if not ENABLE_WIKIPEDIA:
        if DEBUG:
            print("[DEBUG enhanced_search] should_search_wikipedia() → wikipedia disabled, returning False")
        return False
    
    message_lower = message.lower()
    if DEBUG:
        print(f"[DEBUG enhanced_search] should_search_wikipedia() normalized_length={len(message_lower)}")
    
    # Encyclopedic/factual triggers
    wiki_triggers = [
        "what is", "who is", "who was", "what was",
        "define", "definition of",
        "tell me about", "explain",
        "history of", "facts about",
        "information about",
    ]
    
    # Avoid if it's clearly current/time-sensitive
    if should_search_web(message):
        if DEBUG:
            print("[DEBUG enhanced_search] should_search_wikipedia() → should_search_web() is True, returning False")
        return False
    
    match = any(trigger in message_lower for trigger in wiki_triggers)
    if DEBUG:
        print(f"[DEBUG enhanced_search] should_search_wikipedia() matched_triggers={match}")
    return match


def _asks_about_chat_history(query: str) -> bool:
    """Use the conversation, not external pages, for questions about prior chat."""
    shared_intent_available = False
    try:
        from MedlarTV.core.conversation_context import memory_query_topics
        shared_intent_available = True
        if memory_query_topics(query):
            return True
    except Exception:
        # Retain the conservative fallback below if scoped-memory helpers are
        # unavailable; external search must not become the memory authority.
        pass
    text = query.casefold()
    generic_history = bool(
        re.search(r"\b(?:did|have)\s+(?:i|we)\s+(?:tell|told|say|said|ask|asked|mention|mentioned|share|shared|discuss|discussed)\b", text)
        or re.search(r"\b(?:what|which)\b.*\b(?:i|we)\s+(?:told|said|asked|mentioned|shared|discussed)\b", text)
        or re.search(r"\b(?:do|can)\s+you\s+(?:remember|recall)\b.*\b(?:i|we|my)\b", text)
        or re.search(r"\b(?:what|which)(?:'s| is| was)\s+my\s+test\s+word\b", text)
        or re.search(r"\bin (?:this|our) chat\b", text)
        or re.search(r"\b(?:i|we|you)\b.*\b(?:tell|told|say|said|mention|mentioned|discuss|discussed)\b.*\b(?:earlier|previously)\b", text)
        or re.search(
            r"\b(?:what|which|give|list|repeat|recall|remember|remind)\b"
            r".*\b(?:group|chat)(?:['\u2019]s)?\b.*"
            r"\b(?:words?|tokens?|items?|clues?|phrases?|sequences?)\b",
            text,
        )
    )
    if generic_history or shared_intent_available:
        return generic_history
    # Degraded-mode fallback for isolated/minimal deployments where the shared
    # classifier cannot import. The normal runtime never uses this as the
    # authority for memory routing.
    external_referent = re.search(
        r"\b(?:words?|tokens?|items?|clues?|phrases?|sequences?)\s+"
        r"(?:in|to|of)\s+(.+)",
        text,
    )
    if external_referent:
        referent = external_referent.group(1).strip(" ?!.,")
        is_order = re.fullmatch(
            r"(?:reverse|original|alphabetical|numbered)?\s*order"
            r"(?:,?\s+please)?",
            referent,
        )
        is_chat = re.search(
            r"\b(?:chat|channel|stream|conversation|messages?|list|group|"
            r"reply|response|answer)\b",
            referent,
        )
        if not is_order and not is_chat:
            return False
    has_recall_verb = re.search(
        r"\b(?:what|which|give|list|repeat|recall|remember|remind)\b", text,
    )
    has_list_noun = re.search(
        r"\b(?:words?|tokens?|items?|clues?|phrases?|sequences?)\b", text,
    )
    has_memory_referent = re.search(
        r"\b(?:my|our|those|same|group|chat)\b|"
        r"\bthe\s+(?:(?:\d{1,2}|one|two|three|four|five|six|seven|eight|"
        r"nine|ten)(?:\s+|-))?",
        text,
    )
    return bool(has_recall_verb and has_list_noun and has_memory_referent)


def search_intelligently(query: str) -> str:
    """
    Intelligently choose between web search, Wikipedia, or both.
    
    Strategy:
    1. Check if question is about current/time-sensitive info → Web search
    2. Check if question is about facts/definitions → Wikipedia
    3. For general questions ending in "?" → Try web search
    4. Combine both if relevant
    
    Args:
        query: User's message/question
    
    Returns:
        Formatted search context string (empty if no results)
    """
    if DEBUG:
        print(f"[DEBUG enhanced_search] search_intelligently() query_length={len(query)}")

    if _asks_about_chat_history(query):
        return ""

    context_parts = []
    
    # Check if we should search Wikipedia
    wiki_flag = should_search_wikipedia(query)
    if DEBUG:
        print(f"[DEBUG enhanced_search] search_intelligently() should_search_wikipedia={wiki_flag}")
    if wiki_flag:
        if DEBUG:
            print("[DEBUG enhanced_search] search_intelligently() trying Wikipedia")
        print("[Smart Search] Trying Wikipedia")
        wiki_result = search_wikipedia(query)
        if wiki_result:
            if DEBUG:
                print("[DEBUG enhanced_search] search_intelligently() wiki_result found, adding to context_parts")
            context_parts.append("WIKIPEDIA INFORMATION:\n")
            context_parts.append(f"Title: {wiki_result['title']}\n")
            context_parts.append(f"Summary: {wiki_result['summary']}\n")
            context_parts.append(f"Source: {wiki_result['url']}\n")
        else:
            if DEBUG:
                print("[DEBUG enhanced_search] search_intelligently() wiki_result is None")
    
    # Check if we should search the web
    web_flag = should_search_web(query)
    if DEBUG:
        print(f"[DEBUG enhanced_search] search_intelligently() should_search_web={web_flag}")
    if web_flag:
        if DEBUG:
            print("[DEBUG enhanced_search] search_intelligently() trying Web Search")
        print("[Smart Search] Trying Web Search")
        web_results = search_web(query, max_results=3)
        if web_results:
            if DEBUG:
                print(f"[DEBUG enhanced_search] search_intelligently() web_results count={len(web_results)}")
            if context_parts:
                context_parts.append("\n")
            context_parts.append("CURRENT WEB INFORMATION:\n")
            for i, result in enumerate(web_results, 1):
                title = result.get('title', 'N/A')
                body = result.get('body', result.get('description', 'N/A'))
                if DEBUG:
                    print(f"[DEBUG enhanced_search] search_intelligently() adding web result {i}")
                context_parts.append(f"[Result {i}] {title}\n{body}\n\n")
        else:
            if DEBUG:
                print("[DEBUG enhanced_search] search_intelligently() web_results is empty or None")
    
    # If we found nothing but it's a question, try a general web search
    if not context_parts and "?" in query:
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_intelligently() → no context yet and query is question, fallback web search")
        print("[Smart Search] Fallback web search")
        web_results = search_web(query, max_results=2)
        if web_results:
            if DEBUG:
                print(f"[DEBUG enhanced_search] search_intelligently() fallback web_results count={len(web_results)}")
            context_parts.append("WEB SEARCH RESULTS:\n")
            for i, result in enumerate(web_results, 1):
                title = result.get('title', 'N/A')
                body = result.get('body', 'N/A')
                if DEBUG:
                    print(f"[DEBUG enhanced_search] search_intelligently() adding fallback result {i}")
                context_parts.append(f"{title}: {body}\n")
        else:
            print("[DEBUG enhanced_search] search_intelligently() fallback web_results is empty or None")
    
    if context_parts:
        full_context = "".join(context_parts)
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_intelligently() built context length before footer={len(full_context)}")
        full_context += "\n---\nUse these external results only when they help answer the viewer's factual question. For earlier chat, rely on the prior messages. Keep the answer concise and conversational for Twitch chat (1-2 sentences max)."
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_intelligently() final context length={len(full_context)}")
        return full_context
    
    if DEBUG:
        print("[DEBUG enhanced_search] search_intelligently() no context generated, returning empty string")
    return ""


def search_news(query: str, max_results: int = None) -> List[Dict]:
    """
    Search recent news articles using DuckDuckGo News.
    
    Args:
        query: Search query string
        max_results: Maximum number of results (default: from env or 3)
    
    Returns:
        List of news results
    """
    _configure_search()
    if DEBUG:
        print(f"[DEBUG enhanced_search] search_news() query_length={len(query)}, max_results={max_results!r}")

    if not ENABLE_WEB_SEARCH:
        if DEBUG:
            print("[DEBUG enhanced_search] search_news() → web search disabled, returning []")
        return []
    
    if max_results is None:
        max_results = WEB_SEARCH_MAX_RESULTS
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_news() max_results was None, using default={max_results}")
    
    max_results = _positive_result_limit(max_results, WEB_SEARCH_MAX_RESULTS)
    try:
        if DEBUG:
            print("[DEBUG enhanced_search] search_news() creating DDGS() context")
        if DEBUG:
            print("[DEBUG enhanced_search] search_news() calling ddgs.news(...)")
        results = list(_get_ddgs().news(query, max_results=max_results))
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_news() ddgs.news() returned {len(results)} results")
        print(f"[News Search] Found {len(results)} articles")
        return results
    except Exception as e:
        if DEBUG:
            print(f"[DEBUG enhanced_search] search_news() exception_type={type(e).__name__}")
        print(f"[News Search] Error: {type(e).__name__}")
        return []


def format_search_context(results: List[Dict], query: str = "") -> str:
    """
    Format web search results into context string for LLM.
    
    Args:
        results: List of search result dictionaries
        query: Original search query (optional)
    
    Returns:
        Formatted string with search results
    """
    if DEBUG:
        print(f"[DEBUG enhanced_search] format_search_context() results_len={len(results)}, query_length={len(query)}")

    if not results:
        if DEBUG:
            print("[DEBUG enhanced_search] format_search_context() results empty, returning fallback message")
        return "No relevant search results found."
    
    context = "CURRENT WEB INFORMATION:\n"
    if query:
        context += f"Search Query: {query}\n"
    context += "\n"
    
    for i, result in enumerate(results, 1):
        title = result.get('title', 'N/A')
        body = result.get('body', result.get('description', 'N/A'))
        href = result.get('href', '')
        
        if DEBUG:
            print(f"[DEBUG enhanced_search] format_search_context() result {i}")
        context += f"[Result {i}]\n"
        context += f"Title: {title}\n"
        context += f"Content: {body}\n"
        if href:
            context += f"Source: {href}\n"
        context += "\n"
    
    context += "Use this information to provide an accurate, current answer.\n"
    if DEBUG:
        print(f"[DEBUG enhanced_search] format_search_context() final context length={len(context)}")
    
    return context.strip()


# Test function
if __name__ == "__main__":
    print("=" * 60)
    print("Testing MedlarTV Enhanced Search (Web + Wikipedia)")
    print("=" * 60)
    
    # Test 1: Wikipedia
    print("\n=== Test 1: Wikipedia Search ===")
    wiki_test = "Artificial Intelligence"
    print(f"Query: {wiki_test}")
    result = search_wikipedia(wiki_test)
    if result:
        print(f"✅ Found: {result['title']}")
        print(f"Summary: {result['summary'][:150]}...")
        print(f"URL: {result['url']}")
    else:
        print("❌ No Wikipedia result")
    
    # Test 2: Web Search
    print("\n=== Test 2: Web Search (Current Info) ===")
    web_test = "Bitcoin price today"
    print(f"Query: {web_test}")
    results = search_web(web_test, max_results=2)
    if results:
        print(f"✅ Found {len(results)} results")
        for i, r in enumerate(results, 1):
            print(f"  {i}. {r.get('title', 'N/A')[:60]}...")
    else:
        print("❌ No web results")
    
    # Test 3: Smart Search (Factual)
    print("\n=== Test 3: Smart Search (Factual) ===")
    smart_test_1 = "what is quantum computing"
    print(f"Query: {smart_test_1}")
    print(f"Should use Wikipedia? {should_search_wikipedia(smart_test_1)}")
    print(f"Should use Web? {should_search_web(smart_test_1)}")
    context = search_intelligently(smart_test_1)
    if context:
        print(f"✅ Generated context ({len(context)} chars)")
        print(context[:200] + "...")
    else:
        print("❌ No context generated")
    
    # Test 4: Smart Search (Current)
    print("\n=== Test 4: Smart Search (Current Info) ===")
    smart_test_2 = "latest news about AI"
    print(f"Query: {smart_test_2}")
    print(f"Should use Wikipedia? {should_search_wikipedia(smart_test_2)}")
    print(f"Should use Web? {should_search_web(smart_test_2)}")
    context2 = search_intelligently(smart_test_2)
    if context2:
        print(f"✅ Generated context ({len(context2)} chars)")
    else:
        print("❌ No context generated")
    
    # Test 5: Trigger Detection
    print("\n=== Test 5: Trigger Detection ===")
    test_queries = [
        "what is python?",           # Should trigger Wikipedia
        "price of ethereum today?",  # Should trigger Web
        "how are you?",              # Should trigger neither
        "who was Albert Einstein?",  # Should trigger Wikipedia
        "latest bitcoin news",       # Should trigger Web
    ]
    
    for q in test_queries:
        wiki_flag = "✅" if should_search_wikipedia(q) else "❌"
        web_flag = "✅" if should_search_web(q) else "❌"
        print(f"  '{q[:30]:30}' → Wiki:{wiki_flag} Web:{web_flag}")
    
    print("\n" + "=" * 60)
    print("✅ Enhanced Search tests complete!")
    print("=" * 60)
