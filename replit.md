# Overview

MillELO.com is a real-time multiplayer Nine Men's Morris gaming platform. It offers an ELO-based rating system, tournament support, and live game analysis, allowing players to compete online with various time controls (Bullet, Blitz, Rapid), join tournaments, track statistics, and challenge others. The project aims to provide a comprehensive and engaging online experience for this classic board game.

# User Preferences

Preferred communication style: Simple, everyday language.

# System Architecture

## Monolithic Flask Application
The application is built as a monolithic Flask application for rapid development and deployment, initially using in-memory data storage for simplicity.

## Real-Time Communication with WebSockets
Flask-SocketIO is used for real-time, bidirectional communication, enabling instant game state updates, player moves, and tournament notifications. This supports features like live game moves, player presence, tournament timers, and challenge notifications.

## In-Memory Data Storage
Core data such as user profiles, game states, tournaments, and challenges are stored in Python dictionaries in memory. This provides fast access but lacks persistence.

## Session-Based Authentication
Authentication uses Flask session cookies with a server-side secret key. Passwords are hashed using SHA-256 for basic security.

## Jinja2 Template Rendering
Dynamic HTML pages are rendered server-side using Jinja2 templates, providing a structured approach to UI generation.

## ELO Rating System
An ELO rating system is implemented with separate ratings for Bullet and Blitz time controls. It includes a title system (I through V) based on rating thresholds and tracks player statistics.

## Tournament System (Lichess-Style Arena)
The platform features a Lichess-style arena tournament system with instant pairing and continuous matchmaking. Key features include:
- Score-based matchmaking.
- Automatic redirection to new games.
- A 20-second first-move countdown.
- Berserk mode for bonus points.
- Win streak tracking for additional bonuses.
- Pause functionality and automatic re-queueing.
- A sophisticated scoring system based on wins, draws, berserk, and win streaks.
- Server time synchronization for accurate tournament status.

## Game State Management
Game states are managed using nested dictionaries, tracking board positions, move history, timers, and game phases (placement, movement, flying).

## Hierarchical Admin Rank System
A four-tier admin rank system (Admin, Dragon, Galaxy, Creator) is implemented to manage permissions and moderation. Each rank has specific promotion/demotion capabilities and access to commands like banning, kicking, and spawning tournaments. Visual indicators (icons, auras, glows) differentiate admin ranks in the UI.

## Custom Piece Design System
Players can personalize their game pieces from a selection of designs (e.g., star, diamond, heart). These designs are stored per user and rendered as SVG text overlays on the game board, visible to both players.

## In-Game Troll Commands
Players can use "troll commands" during games to send meme popups and temporary "kicks" to opponents. These commands are accompanied by synthesized sound effects and visual overlays, designed for fun and interaction.

## Friends System
A comprehensive friends system enables social features:
- Friend requests: Send, accept, or reject friend requests from player profiles
- Friends list: View all friends with online status indicators
- Private messaging: Real-time chat between friends with message persistence
- Notifications: Real-time socket events for friend requests and new messages
- Navigation: Friends button in both desktop nav and mobile hamburger menu
- Database tables: Friendship (user_id, friend_id, status, created_at) and PrivateMessage (sender_id, receiver_id, message, timestamp, is_read)

## Tournament Display
Admin-created tournaments appear at the top of the tournament list with distinctive styling (black glow, white aura, crown icon). Tournaments are sorted by start time within their category.

# External Dependencies

- **Flask Framework**: (Version 2.3.3) Used for core web application functionalities, routing, and session management.
- **Flask-SocketIO**: (Version 5.3.6) Integrates WebSockets for real-time communication.
- **python-socketio & python-engineio**: (Versions 5.8.0, 4.7.1) Provide low-level Socket.IO protocol implementation.
- **Font Awesome**: Icon library for UI elements (loaded via CDN).
- **Socket.IO Client Library**: (Version 4.0.1) Client-side WebSocket communication in the browser (loaded via CDN).
- **PostgreSQL Database**: Used for persistent storage of user accounts, ratings, and admin ranks.