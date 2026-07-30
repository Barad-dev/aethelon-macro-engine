Project: Aethelon Macro Engine



Current Status (Last Updated: July 30, 2026):

\- Stage A (Infrastructure \& Core): Centralized logging system in core/logger.py with 7-day rotation, Pydantic v2 schemas in models/desk\_schemas.py.

\- Stage B1 (Async Client): Resilient AsyncHttpClient using httpx and asyncio with custom retry policy and network exception handling.

\- Stage B2 (Drivers \& Watermark): WatermarkManager for JSON state persistence, along with RSSDriver, ForexFactoryDriver, and FREDDriver. (Tag v0.3.2-Alpha).



Remaining Tasks:

\- Stage B3: Decouple the data ingestion pipeline from NLP processing and storage layers.

\- Stage C: Macro processing engine, text analytics, and analytical database integration.



Project Rules:

\- Tech Stack: Python 3.11+ using httpx, asyncio, and Pydantic v2.

\- Code Standards: Strict type hints and clear docstrings across all functions, classes, and methods.

\- Timezone Enforcement: All timestamps across all layers must strictly be timezone-aware UTC in ISO 8601 Z format.

\- Architectural Boundaries: Ingestion drivers must never write directly to the analytical database; they return normalized dictionary items (NormalizedItem).

\- Security: Never commit .env files, API keys (e.g., FRED API keys), or runtime state files.



Technical Notes:

\- Log \& State Storage Path: %APPDATA%\\Aethelon\\ on Windows or \~/.aethelon/ on Linux/macOS.

\- Watermark File Path: %APPDATA%\\Aethelon\\state\\watermarks.json

