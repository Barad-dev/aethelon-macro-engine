================================================================================
         AETHELON MACRO ENGINE - ARCHITECTURE ROADMAP (v2.5)
                 [Institutional Polyglot Edition]
================================================================================

[OVERVIEW SUMMARY]
┌─────────┬───────────────────────────────────┬───────────────────────────────────────────────────────────────┐
│ Stage   │ Module Title                      │ Core Focus & Key Deliverables                                 │
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────────────────────┤
│ Stage A │ Core Refactoring & Data Contracts │ AppData, Logging & Polyglot-Ready Pydantic v2 Schemas         │
│ Stage B │ Resilient Async Ingestion Engine  │ Decoupled Async Stream Ingestion (Go-Ready Event Architecture)│
│ Stage C │ Causal Reasoning & Macro Logic    │ Macro Regime Decision Trees, Hard/Soft Invalidation Logic     │
│ Stage D │ 3-Layer Storage Architecture      │ RAM Cache, SQLite WAL Mode Store (Multi-Process Concurrent)   │
│ Stage E │ Execution, IPC Bridge & Visuals   │ MT5 ZeroMQ/gRPC IPC Bridge & 5 Fundamental Visual Overlays    │
│ Stage F │ Process Isolation & Async Shell   │ Multi-threaded PySide6 GUI & Zero-Freeze Event Loop           │
│ Stage G │ Master Prompt & Intermediary AI   │ Offline Intermediary AI Audit, Shock Parser & Master Rules    │
│ Stage H │ Polyglot Engine & Institutional UI│ Python Intelligence, Go/Rust/C++ Rewrite & 7-Day Stress Run   │
└─────────┴───────────────────────────────────┴───────────────────────────────────────────────────────────────┘

--------------------------------------------------------------------------------
ARCHITECTURAL PLUMBING DIRECTIVES (STAGES A - E)
--------------------------------------------------------------------------------
• Polyglot Data Contracts (Stage A):
  - Enforce clean, language-agnostic Pydantic v2 serialization (JSON/Protobuf compatible) 
    to enable zero-overhead data exchange between Python, Go, and Rust services.
• Decoupled Ingestion Pipeline (Stage B):
  - Structure news and FRED data fetchers as independent event-driven pipelines, 
    allowing seamless drop-in replacement with Go native microservices.
• Concurrent WAL Mode Database (Stage D):
  - Configure SQLite in Write-Ahead Logging (WAL Mode) to guarantee non-blocking, 
    multi-process read/write access across Python orchestrators and Rust/Go engines.
• Universal Execution IPC (Stage E):
  - Decouple MetaTrader 5 execution via socket-based IPC protocols (ZeroMQ / gRPC / WebSockets) 
    instead of Python-bound native wrappers, ensuring cross-language execution capabilities.

--------------------------------------------------------------------------------
DETAILED STAGE SPECIFICATIONS
--------------------------------------------------------------------------------

🟢 STAGE A: CORE REFACTORING, APPDATA & CENTRAL LOGGING MODULE
• Workspace Purge:
  - Complete deletion of obsolete legacy files (Tkinter GUI, test harnesses, stale __pycache__).
  - Removal of all conditional fallback logic in run_engine.py.
• AppData Centralization:
  - Centralize path resolution in a dedicated paths/config module.
  - Automatically resolve and migrate news_engine_store.db to Windows %APPDATA%\Aethelon\ 
    (with cross-platform fallback to ~/.aethelon).
• Polyglot-Ready Pydantic v2 Schemas (models/desk_schemas.py):
  - Strict type checking for all desk payloads (MacroState, InstrumentThesis, EventStudyItem).
  - Native export serialization (JSON / IPC ready) ensuring seamless parsing by Go/Rust.
  - Schema inclusion of Layman/Actionable fields adapted to active system language:
    1) layman_meaning ("یعنی"): Plain-language translation of complex macro logic.
    2) market_impact ("در نتیجه"): Direct directional bias, probability, and execution implications.
• Centralized Logging Subsystem (utils/logger.py):
  - Dedicated logging architecture storing runtime output in %APPDATA%\Aethelon\logs\app.log.
  - Automated 7-day rolling rotation (TimedRotatingFileHandler with backupCount=7) to prune legacy logs.
  - Multi-level severity formatting (DEBUG, INFO, WARNING, ERROR, CRITICAL).
  - Structured output schema prepared for live real-time GUI streaming in Stage H.
• Log-Based Stage Verification Protocol:
  - End-of-stage verification process validating log output before advancing to subsequent stages.

🟢 STAGE B: RESILIENT ASYNC INGESTION ENGINE
• Async Network Overhaul:
  - Migrate all data collection drivers (RSS feeds, ForexFactory calendar, FRED economic metrics) 
    from synchronous blocking code to async execution (httpx / asyncio).
• Network Fault Tolerance:
  - Implement Exponential Backoff with Retry Logic to handle rate limits (HTTP 429) and network timeouts.
  - Decouple ingestion workers from internal NLP analysis pipelines to prevent processing bottlenecks.

🟢 STAGE C: CAUSAL REASONING & MACRO LOGIC ENGINE
• Macro Regime Decision Trees:
  - Core logic engine classifying economic conditions into 4 macro regimes: 
    Reflation, Stagflation, Goldilocks, Deflation.
• Invalidation Logic Framework:
  - Hard Invalidation: Structural shifts in core FRED macroeconomic indicators triggering thesis re-evaluation.
  - Soft Divergence (Tactical Divergence): Price action diverging due to temporary noise while fundamental macro drivers remain intact.
• Exogenous Shock Isolator:
  - Logic sub-routine detecting non-macro interruptions (e.g., geopolitical shocks, emergency central bank interventions) 
    to preserve core algorithmic integrity during temporary price anomalies.

🟢 STAGE D: 3-LAYER STORAGE ARCHITECTURE
• L1 Hot Cache (In-Memory RAM):
  - Ultra-fast volatile memory store for live UI dashboard rendering.
• L2 Warm Store (Local SQLite in WAL Mode):
  - 30-day transactional history for pattern analysis, event studies, and news lookups.
  - WAL Mode enabled for simultaneous multi-process access across Python and compiled binaries.
• L3 Cold Vault (Compressed Archive):
  - Long-term storage vault optimized for historical backfilling (via backfill_macro_history.py) 
    and multi-year macro regime backtesting.

🟢 STAGE E: EXECUTION, LOCAL API IPC BRIDGE & CHART OVERLAYS (MT5 & TRADINGVIEW)
• Dual Platform Connectivity:
  - MT5 Integration: Decoupled IPC execution via ZeroMQ / gRPC / WebSockets sockets.
  - TradingView Integration: Local FastAPI / Webhook Server serving Pine Script indicators 
    and embedded TradingView Lightweight Charts engine.
• 5 Core Fundamental Visual Chart Overlays:
  1) Smart Event Pins on Candles:
     - Interactive candle-level icons marking major release events (CPI, NFP, Rate Decisions).
     - Hover metrics: Surprise Factor (Actual vs. Forecast) + 5-minute Pip movement reaction.
  2) Macro Bias Ribbon:
     - Top-of-chart visual ribbon displaying real-time economic environment:
       • Green (Risk-On): Growth environment / Risk asset expansion.
       • Red (Risk-Off): Contraction / Flight to safe-haven assets (USD, Gold).
       • Orange (Stagflation / Uncertainty): Mixed monetary policy signals.
  3) Fundamental Divergence Overlay:
     - Faded indicator overlays (US10Y yields, DXY) directly on asset charts (e.g., EUR/USD).
     - Automatic alert generation when price action diverges from yield/fundamental drivers ("Price Trap Warning").
  4) Pre-Event Playbook Zones:
     - Pre-news (e.g., 10m before FOMC/CPI) automated drawing of 3 dynamic scenario boxes on future chart space:
       • Box A (Hawkish/Restrictive): Target price zone X.
       • Box B (In-Line/Expected): Range bounds Y to Z.
       • Box C (Dovish/Accommodative): Target price zone W.
  5) Fundamental Signal Filtering (Macro Bias Enforcement):
     - Check technical trade setups (order blocks, trendline breaks) against macro thesis:
       • Aligned Setups ──► High Probability (Highlighted signal).
       • Counter-Macro Setups ──► High Risk / Counter-Macro warning tag (Faded signal).

🟢 STAGE F: PROCESS ISOLATION & ASYNC SHELL
• Multi-Threaded GUI Architecture:
  - Isolate the PySide6 Qt GUI loop onto a dedicated main UI thread.
  - Offload heavy asynchronous background NLP tasks and database disk I/O to background workers.
• Zero-Freeze Guarantee:
  - Ensure the dashboard remains 100% responsive during high-volume news spikes or full-history backfills.

🟢 STAGE G: MASTER PROMPT & INTERMEDIARY AI DISTILLATION LOOP
• Intermediary Evaluator AI (Offline Audit Agent):
  - Independent AI layer analyzing 1-month and 6-month historical news archives alongside actual price outcomes.
  - Audits divergence between macro thesis predictions and real-world market movements.
• Exogenous Shock Parser & Rule Preservation:
  - Categorizes market anomalies as exogenous shocks without corrupting proven underlying macro rules.
• Master Prompt Distillation:
  - Synthesizes long-term historical insights into compact, high-density system prompts (Master Rules).
• Dynamic Language Target Output:
  - Structured AI outputs dynamically matching the target language configured in Stage A/H.

🟢 STAGE H: INSTITUTIONAL UI, PYTHON INTELLIGENCE & UNCOMPROMISING POLYGLOT REWRITE
• Modern Dark Slate Styling:
  - Apply custom QSS stylesheets with dark slate (#1E222A / #181A1F) color palettes, 
    sharp borders, clean typography, and dedicated card layouts.
• Full Application Language Switching:
  - Default: 100% English interface and prompt output.
  - Optional Toggle: 100% Persian interface with complete Qt string translation (QTranslator), 
    RTL layout support, and Persian AI prompt responses.
• Live System Log & Health Monitor UI:
  - Dedicated PySide6 dashboard panel for real-time log streaming, system status, resource metrics, and anomaly flags.
• Long-Term Endurance Testing (3 to 7 Days):
  - Continuous 3 to 7-day endurance run before v1.0.0 release to audit memory stability, connection resilience, 
    and multi-day algorithmic precision.
• Python Code Intelligence Audit:
  - Systematic review of all Python modules to replace naive implementations with highly intelligent, 
    memory-efficient code (e.g., in-place operations, eliminating redundant allocations/conversions, clean iterators).
• Deep Polyglot Rewrite & Maximum Limit Engineering (Go / Rust / C++):
  - Post-core architectural rewrite push to optimize every capable subsystem to its absolute technical limits:
    1) High-Concurrency Ingestion & Streaming (Go): Re-engineer network drivers, RSS parsers, 
       and WebSocket ingestion into a dedicated ultra-fast Go service.
    2) Heavy Computation, Rescoring & Pattern Engines (Rust): Rewrite historical backtesting, 
       causality matrix calculations, and math-heavy modules in Rust via PyO3 / C-ABI bindings for zero-copy efficiency.
    3) Low-Latency Native Plugins (C++ Optional): Write native DLL / shared object extensions for MT5 
       if sub-millisecond execution mapping is required.
================================================================================