from flask import render_template, request, redirect, url_for, session, jsonify, flash, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
import hashlib
import json
import random
import time
from datetime import datetime, timedelta
import uuid
import threading
from urllib.parse import unquote
import os

from app import app

from models import db, User, Game, Friendship, PrivateMessage, BanRecord, ArchivedTournament
if 'sqlalchemy' not in app.extensions:
    db.init_app(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

@app.route('/attached_assets/<path:filename>')
def serve_attached_assets(filename):
    return send_from_directory('attached_assets', filename)

# Custom Jinja2 filters
def hex_to_rgb(hex_color):
    """Convert hex color to RGB values"""
    if hex_color.startswith('#'):
        hex_color = hex_color[1:]
    return f"{int(hex_color[0:2], 16)}, {int(hex_color[2:4], 16)}, {int(hex_color[4:6], 16)}"

# Add tojson filter for templates
import json

def tojson_filter(value):
    """Convert Python object to JSON string"""
    return json.dumps(value)

app.jinja_env.filters['hex_to_rgb'] = hex_to_rgb
app.jinja_env.filters['tojson'] = tojson_filter

# Make variables available to all templates
@app.context_processor
def inject_globals():
    import json
    username = session.get('username')
    admin_rank = None
    admin_commands_json = '{}'
    if username and username in users:
        admin_rank = users[username].get('admin_rank')
        if admin_rank:
            admin_commands_json = json.dumps(get_commands_for_rank(admin_rank))
    return {
        'users': users,
        'get_title': get_title,
        'get_ranking_color': get_ranking_color,
        'admin_rank': admin_rank,
        'admin_commands_json': admin_commands_json
    }

# Handle URL encoding issues (e.g., %3F instead of ?)
@app.errorhandler(404)
def handle_encoded_url(error):
    path = request.path
    if '%3F' in path or '%3f' in path:
        decoded_path = unquote(path)
        if '?' in decoded_path:
            parts = decoded_path.split('?', 1)
            query_string = parts[1] if len(parts) > 1 else ''
            if request.query_string:
                query_string = query_string + '&' + request.query_string.decode('utf-8') if query_string else request.query_string.decode('utf-8')
            new_url = parts[0] + ('?' + query_string if query_string else '')
            return redirect(new_url, code=302)
    return render_template('404.html'), 404

# In-memory storage (in production, use a proper database)
users = {}
games = {}
tournaments = {}
archived_tournaments = {}  # Store finished tournaments for trophy links
admin_tournament_counter = 0  # Counter for admin tournament naming (admin1, admin2, etc.)
admin_tournament_invites = {}  # {username: [list of tournament_ids they're invited to]}
tournament_chats = {}  # {tournament_id: [{username, message, timestamp}, ...]}
game_rooms = {}
online_users = {}  # sid -> username mapping
banned_users = set()
paused_users = set()
players_in_game_menu = set()  # Players viewing the after-game menu (can't be paired)
game_menu_timestamps = {}  # {username: timestamp} - track when player entered game menu (for timeout)
admin_commands_visible = {}
challenges = {}  # Global challenges dictionary
tournament_recent_opponents = {}  # Track last opponent per player per tournament: {tournament_id: {player: last_opponent}}
tournament_page_users = {}  # username -> tournament_id for users currently on tournament page
pending_auto_pause = set()  # Users who should be auto-paused after their current game ends
auto_paused_users = set()  # Users who were auto-paused (vs manually paused)
pending_2fa_codes = {}  # username -> {'code': '123456', 'expires': timestamp}
friendships = {}  # {id: {user1, user2, status, created}}
private_messages = {}  # {id: {sender, receiver, message, timestamp, read}}
friend_requests = {}  # {receiver_username: [{from_user, timestamp}, ...]}

def init_admin_tournament_counter():
    """Initialize admin tournament counter based on existing admin tournaments."""
    global admin_tournament_counter
    import re
    max_counter = 0
    for t in tournaments.values():
        if t.get('admin_only') and t.get('name', '').startswith('admin'):
            match = re.search(r'admin(\d+)', t.get('name', ''))
            if match:
                num = int(match.group(1))
                if num > max_counter:
                    max_counter = num
    admin_tournament_counter = max_counter

# Timer management - Server-authoritative system
game_timers = {}  # game_id -> timer_info
timer_lock = threading.Lock()

# Timer update interval (seconds)
TIMER_UPDATE_INTERVAL = 1.0
TIMER_SYNC_INTERVAL = 5.0  # Send full sync every 5 seconds

# Timer states
TIMER_RUNNING = 'running'
TIMER_PAUSED = 'paused'
TIMER_EXPIRED = 'expired'

# Database helper functions
def save_user_to_db(username):
    """Save a user's data to the database"""
    if username not in users:
        return
    user_data = users[username]
    with app.app_context():
        db_user = User.query.filter_by(username=username).first()
        if db_user:
            db_user.update_from_dict(user_data)
        else:
            db_user = User(username=username)
            db_user.update_from_dict(user_data)
            db.session.add(db_user)
        db.session.commit()

def save_game_to_db(game_id):
    """Save a finished game to the database"""
    if game_id not in games:
        return
    game_data = games[game_id]
    if game_data.get('status') != 'finished':
        return
    with app.app_context():
        db_game = Game.query.filter_by(id=game_id).first()
        if not db_game:
            db_game = Game(
                id=game_id,
                white=game_data.get('white'),
                black=game_data.get('black'),
                winner=game_data.get('winner'),
                status=game_data.get('status', 'finished'),
                end_reason=game_data.get('end_reason'),
                time_control=game_data.get('time_control'),
                rating_type=game_data.get('rating_type'),
                start_time=game_data.get('start_time'),
                end_time=game_data.get('end_time'),
                moves=game_data.get('moves', []),
                rating_changes=game_data.get('rating_changes', {}),
                tournament_id=game_data.get('tournament_id'),
                white_berserk=game_data.get('white_berserk', False),
                black_berserk=game_data.get('black_berserk', False),
                positions=game_data.get('positions', [])
            )
            db.session.add(db_game)
            db.session.commit()
            print(f"Saved game {game_id} to database")

def load_games_from_db():
    """Load all finished games from database into memory"""
    with app.app_context():
        for db_game in Game.query.all():
            games[db_game.id] = db_game.to_dict()
        print(f"Loaded {len(games)} games from database")

def load_users_from_db():
    """Load all users from database into memory"""
    with app.app_context():
        for db_user in User.query.all():
            users[db_user.username] = db_user.to_dict()
        
        # Ensure admin account exists
        if 'Frut' not in users:
            admin_data = {
                'username': 'Frut',
                'password': hashlib.sha256('Filip20111'.encode()).hexdigest(),
                'bullet_rating': 100,
                'blitz_rating': 100,
                'games_played': {'bullet': 0, 'blitz': 0},
                'wins': {'bullet': 0, 'blitz': 0},
                'losses': {'bullet': 0, 'blitz': 0},
                'draws': {'bullet': 0, 'blitz': 0},
                'created': datetime.now().isoformat(),
                'color': '#FF0000',
                'is_admin': True,
                'admin_rank': 'creator',
                'best_wins': {'bullet': [], 'blitz': []},
                'tournaments_won': {'daily': 0, 'weekly': 0, 'monthly': 0, 'marathon': 0, 'world_cup': 0},
                'trophies': [],
                'elo_history': {'bullet': [], 'blitz': []},
                'highest_title': None,
                'highest_title_color': '#888888'
            }
            users['Frut'] = admin_data
            db_user = User(username='Frut')
            db_user.update_from_dict(admin_data)
            db.session.add(db_user)
            db.session.commit()
        else:
            # Ensure Frut always has creator rank
            if users['Frut'].get('admin_rank') != 'creator':
                users['Frut']['admin_rank'] = 'creator'
                users['Frut']['is_admin'] = True
                save_user_to_db('Frut')
        print(f"Loaded {len(users)} users from database")

def load_archived_tournaments_from_db():
    """Load archived tournaments from database into memory"""
    with app.app_context():
        for at in ArchivedTournament.query.all():
            archived_tournaments[at.id] = at.to_dict()
        print(f"Loaded {len(archived_tournaments)} archived tournaments from database")

def load_banned_users_from_db():
    """Load banned users from database into memory"""
    with app.app_context():
        active_bans = BanRecord.query.filter_by(is_active=True).all()
        for ban in active_bans:
            banned_users.add(ban.banned_user)
        print(f"Loaded {len(banned_users)} banned users from database")

app_ready = False

def load_all_data():
    global app_ready
    load_users_from_db()
    load_games_from_db()
    load_archived_tournaments_from_db()
    load_banned_users_from_db()
    app_ready = True

# Rating titles - No title for low ratings
TITLES = [
    (500, "I", "#8B4513"),
    (1000, "G", "#4B0082"),
    (1500, "L", "#006400"),
    (2000, "M", "#DC143C"),
    (2300, "D", "#0000FF"),
    (2500, "O", "#008000"),
    (2700, "SU", "#FFD700"),
    (3000, "V", "#FF8C00")
]

# Tournament types
TOURNAMENT_TYPES = {
    'daily': {'duration': 60, 'frequency': 63, 'color': '#4CAF50', 'name': 'Daily Arena'},
    'weekly': {'duration': 180, 'frequency': 10080, 'color': '#2196F3', 'name': 'Weekly Arena'},  
    'monthly': {'duration': 360, 'frequency': 43200, 'color': '#F44336', 'name': 'Monthly Arena'},
    'marathon': {'duration': 1440, 'frequency': 129600, 'color': '#9C27B0', 'name': 'Marathon Arena'},
    'world_cup': {'duration': 10080, 'frequency': 525600, 'color': '#FFD700', 'name': 'Mill World Cup'}  # 7 days, once per year
}

# Admin rank hierarchy (lowest to highest)
ADMIN_RANKS = ['admin', 'dragon', 'galaxy', 'creator']
ADMIN_RANK_LEVELS = {'admin': 1, 'dragon': 2, 'galaxy': 3, 'creator': 4}

def get_admin_rank_level(rank):
    """Get numeric level of an admin rank (0 for non-admin)"""
    if not rank:
        return 0
    return ADMIN_RANK_LEVELS.get(rank, 0)

def can_promote_to(promoter_rank, target_rank):
    """Check if promoter can promote someone to target rank"""
    promoter_level = get_admin_rank_level(promoter_rank)
    target_level = get_admin_rank_level(target_rank)
    # Can only promote to ranks lower than your own
    return promoter_level > target_level and target_level > 0

def can_demote(demoter_rank, target_rank):
    """Check if demoter can demote someone with target rank"""
    demoter_level = get_admin_rank_level(demoter_rank)
    target_level = get_admin_rank_level(target_rank)
    # Can only demote ranks lower than your own (creator can never be demoted)
    if target_rank == 'creator':
        return False
    return demoter_level > target_level

# Admin commands by rank (empty for now, will be filled later)
ADMIN_COMMANDS = {
    'close': 'Close admin commands window',
    'ban [player] [reason]': 'Ban a player permanently (optional reason)',
    'unban <username>': 'Unban a player',
    'setcolourname <username> <color>': 'Change player name color (e.g. setcolourname Frut #ff0000)',
    'like <username>': 'Leave a like on someone\'s profile',
    'spawntournament [duration]': 'Spawn arena with your name (e.g., spawntournament 2.30 for 2h 30m, max 3h)',
    'boardsetup': 'Open piece design customization window',
    'createadmintournament': 'Create admin-only tournament (admin1, admin2, etc.)',
    'invite <username> to <tournament>': 'Invite non-admin to admin tournament (e.g. invite Frut to admin1)',
    'banlist': 'Open the ban list (recent bans & banned players)'
}

DRAGON_COMMANDS = {
    'promote <username> admin': 'Promote user to Admin rank',
    'demote <username>': 'Demote Admin rank user',
    'announce <message>': 'Send announcement to all players'
}

GALAXY_COMMANDS = {
    'promote <username> <rank>': 'Promote user to Admin or Dragon rank',
    'demote <username>': 'Demote Admin or Dragon rank user'
}

CREATOR_COMMANDS = {
    'setelo <username> <rating_type:value>': 'Set player rating (e.g. setelo Frut blitz:2000)',
    'removetitle <username>': 'Remove all titles from a player (e.g. removetitle Frut)',
    'reset <username>': 'Reset player statistics',
    'createtournament <type> <time_control>': 'Create tournament (e.g. createtournament weekly 1+0). Types: daily, weekly, monthly, marathon, worldcup',
    'endtournament <tournament_id>': 'Force end an active tournament (use short ID)',
    'listtournaments': 'List all active tournaments with their IDs',
    'promote <username> <rank>': 'Promote user to any rank (admin/dragon/galaxy)',
    'demote <username>': 'Demote any admin rank user (except creator)'
}

def get_commands_for_rank(rank):
    """Get all available commands for a given admin rank"""
    if not rank:
        return {}
    commands = dict(ADMIN_COMMANDS)
    if rank == 'dragon':
        commands.update(DRAGON_COMMANDS)
    elif rank == 'galaxy':
        commands.update(DRAGON_COMMANDS)
        commands.update(GALAXY_COMMANDS)
    elif rank == 'creator':
        commands.update(DRAGON_COMMANDS)
        commands.update(GALAXY_COMMANDS)
        commands.update(CREATOR_COMMANDS)
    return commands

def get_title(rating, user_data=None):
    """Get title based on rating, titles are permanent once earned"""
    # Find current title based on rating
    current_title = None
    current_title_color = None

    for min_rating, title, color in reversed(TITLES):
        if rating >= min_rating:
            current_title = title
            current_title_color = color
            break

    if user_data:
        # Get the highest title ever achieved
        highest_title = user_data.get('highest_title')
        highest_title_color = user_data.get('highest_title_color', '#888888')

        # If current title is better than stored highest, update it
        if current_title:
            current_title_index = next((i for i, (r, t, c) in enumerate(TITLES) if t == current_title), -1)
            stored_title_index = next((i for i, (r, t, c) in enumerate(TITLES) if t == highest_title), -1)

            if current_title_index > stored_title_index:
                user_data['highest_title'] = current_title
                user_data['highest_title_color'] = current_title_color
                highest_title = current_title
                highest_title_color = current_title_color
                print(f"Updated {user_data.get('username', 'Unknown')} highest title to {current_title}")

        # ALWAYS Return the highest title ever achieved (permanent) - never lose it
        if highest_title and highest_title != 'Beginner':
            return highest_title, highest_title_color

    # For users without data, return current title if any
    if current_title:
        return current_title, current_title_color
    return None, None

def get_rating_type(time_control):
    """Determine rating type based on time control"""
    if time_control in ['1+0', '1+1', '2+1']:
        return 'bullet'
    elif time_control in ['3+0', '3+2', '5+0']:
        return 'blitz'
    return 'blitz'  # Default to blitz

def get_leaderboard_rankings():
    """Get top 3 rankings for Bullet and Blitz leaderboards"""
    bullet_top3 = {}
    blitz_top3 = {}
    
    # Get all users sorted by bullet rating (exclude banned users)
    bullet_sorted = sorted(
        [(username, user_data.get('bullet_rating', 100)) for username, user_data in users.items() if username not in banned_users],
        key=lambda x: x[1],
        reverse=True
    )
    for i, (username, rating) in enumerate(bullet_sorted[:3]):
        bullet_top3[username] = i + 1  # 1, 2, or 3
    
    # Get all users sorted by blitz rating (exclude banned users)
    blitz_sorted = sorted(
        [(username, user_data.get('blitz_rating', 100)) for username, user_data in users.items() if username not in banned_users],
        key=lambda x: x[1],
        reverse=True
    )
    for i, (username, rating) in enumerate(blitz_sorted[:3]):
        blitz_top3[username] = i + 1  # 1, 2, or 3
    
    return bullet_top3, blitz_top3

def get_ranking_badge(username):
    """Get ranking badge for a user [B1/B2/B3] or [R1/R2/R3]"""
    bullet_top3, blitz_top3 = get_leaderboard_rankings()
    badges = []
    
    if username in bullet_top3:
        rank = bullet_top3[username]
        badges.append(f'B{rank}')
    
    if username in blitz_top3:
        rank = blitz_top3[username]
        badges.append(f'R{rank}')
    
    return badges

def get_ranking_color(username):
    """Get the best ranking color for a user based on leaderboard placement
    Returns color or None. Admin ranks keep their own color (no ranking color override).
    Priority: 1st = 3, 2nd = 2, 3rd = 1, no ranking = 0
    """
    # Admin ranks keep their own color - no ranking color override
    user_data = users.get(username, {})
    if user_data.get('admin_rank') in ADMIN_RANKS:
        return None
    
    bullet_top3, blitz_top3 = get_leaderboard_rankings()
    
    # Realistic metallic colors
    GOLD_COLOR = '#D4AF37'      # Realistic metallic gold
    SILVER_COLOR = '#A8A9AD'    # Realistic silver  
    BRONZE_COLOR = '#B87333'    # Realistic copper/bronze
    
    best_priority = 0
    best_color = None
    
    # Check bullet ranking
    if username in bullet_top3:
        rank = bullet_top3[username]
        if rank == 1 and best_priority < 3:
            best_priority = 3
            best_color = GOLD_COLOR
        elif rank == 2 and best_priority < 2:
            best_priority = 2
            best_color = SILVER_COLOR
        elif rank == 3 and best_priority < 1:
            best_priority = 1
            best_color = BRONZE_COLOR
    
    # Check blitz ranking
    if username in blitz_top3:
        rank = blitz_top3[username]
        if rank == 1 and best_priority < 3:
            best_priority = 3
            best_color = GOLD_COLOR
        elif rank == 2 and best_priority < 2:
            best_priority = 2
            best_color = SILVER_COLOR
        elif rank == 3 and best_priority < 1:
            best_priority = 1
            best_color = BRONZE_COLOR
    
    return best_color

def get_featured_game():
    """Get the best currently playing game by combined ELO for lobby display"""
    best_game = None
    best_combined_elo = 0
    
    for game_id, game in games.items():
        status = game.get('status', '')
        # Skip finished/abandoned games, but include active games (playing, waiting, or with active timer)
        if status in ['finished', 'abandoned']:
            continue
        # Also skip games without a timer (not really active)
        if game_id not in game_timers:
            continue
        
        white = game.get('white')
        black = game.get('black')
        if not white or not black:
            continue
        
        white_data = users.get(white, {})
        black_data = users.get(black, {})
        
        time_control = game.get('time_control', '3+2')
        rating_type = get_rating_type(time_control)
        
        white_rating = white_data.get(f'{rating_type}_rating', 1500)
        black_rating = black_data.get(f'{rating_type}_rating', 1500)
        combined_elo = white_rating + black_rating
        
        if combined_elo > best_combined_elo:
            best_combined_elo = combined_elo
            
            white_title, white_title_color = get_title(white_rating, white_data)
            black_title, black_title_color = get_title(black_rating, black_data)
            
            timer = game_timers.get(game_id)
            white_time = 0
            black_time = 0
            if timer:
                times = timer.get_current_times()
                white_time = int(times['white'])
                black_time = int(times['black'])
            
            # Get the actual game object
            game_obj = game.get('game')
            board = game_obj.board if game_obj else [0] * 24
            current_player = game_obj.current_player if game_obj else 'white'
            phase = game_obj.phase if game_obj else 1
            
            # Use ranking color if available, otherwise use user color
            white_ranking_color = get_ranking_color(white)
            black_ranking_color = get_ranking_color(black)
            
            best_game = {
                'game_id': game_id,
                'white': white,
                'black': black,
                'white_rating': white_rating,
                'black_rating': black_rating,
                'white_color': white_ranking_color or white_data.get('color', '#c9c9c9'),
                'black_color': black_ranking_color or black_data.get('color', '#888888'),
                'white_title': white_title,
                'white_title_color': white_title_color,
                'black_title': black_title,
                'black_title_color': black_title_color,
                'time_control': time_control,
                'board': board,
                'current_player': current_player,
                'phase': phase,
                'white_time': white_time,
                'black_time': black_time,
                'combined_elo': combined_elo
            }
    
    return best_game

def initialize_highest_titles():
    """Initialize highest titles for existing users who don't have them"""
    for username, user_data in users.items():
        if 'highest_title' not in user_data:
            # Find highest title based on current ratings
            max_rating = max(user_data.get('bullet_rating', 100), user_data.get('blitz_rating', 100))
            highest_title = None
            highest_title_color = None

            for min_rating, title, color in reversed(TITLES):
                if max_rating >= min_rating:
                    highest_title = title
                    highest_title_color = color
                    break

            # Set the highest title (None if no title earned)
            user_data['highest_title'] = highest_title
            user_data['highest_title_color'] = highest_title_color

class LichessStyleTimer:
    """Lichess-style server-authoritative timer system"""

    def __init__(self, game_id, white_time, black_time, increment=0):
        self.game_id = game_id
        self.white_time = float(white_time)  # Keep as float for precision
        self.black_time = float(black_time)
        self.increment = increment
        self.active_player = 'white'
        self.state = TIMER_RUNNING
        self.last_update = time.time()
        self.last_sync = time.time()
        self.move_count = 0
        self.game_start_time = time.time()

    def get_current_times(self):
        """Get current timer values - Lichess style with precise timing"""
        current_time = time.time()

        if self.state == TIMER_RUNNING:
            elapsed = current_time - self.last_update

            # Deduct time from active player
            if self.active_player == 'white':
                self.white_time = max(0, self.white_time - elapsed)
            else:
                self.black_time = max(0, self.black_time - elapsed)

        self.last_update = current_time

        # Return times in milliseconds (Lichess style) but convert to seconds for display
        return {
            'white': max(0, self.white_time),
            'black': max(0, self.black_time),
            'server_time': current_time
        }

    def switch_player(self, new_player):
        """Switch active player and add increment - Lichess style"""
        if self.state != TIMER_RUNNING:
            return False

        # Update current times first
        current_time = time.time()
        elapsed = current_time - self.last_update

        # Deduct time from current active player
        if self.active_player == 'white':
            self.white_time = max(0, self.white_time - elapsed)
        else:
            self.black_time = max(0, self.black_time - elapsed)

        # Add increment to the player who just moved (always apply if increment > 0)
        increment_applied = False
        if self.increment > 0 and self.move_count >= 0:  # Apply increment from first move
            if self.active_player == 'white':
                old_time = self.white_time
                self.white_time += self.increment
                increment_applied = True
                print(f"TIMER INCREMENT: Added {self.increment} seconds to white. {old_time:.1f} -> {self.white_time:.1f}")
            else:
                old_time = self.black_time
                self.black_time += self.increment
                increment_applied = True
                print(f"TIMER INCREMENT: Added {self.increment} seconds to black. {old_time:.1f} -> {self.black_time:.1f}")

        # Switch to new player
        previous_player = self.active_player
        self.active_player = new_player
        self.move_count += 1
        self.last_update = current_time

        print(f"Player switch: {previous_player} -> {new_player}, Increment applied: {increment_applied}, Increment value: {self.increment}")
        return increment_applied

    def pause(self):
        """Pause the timer"""
        if self.state == TIMER_RUNNING:
            self.get_current_times()
            self.state = TIMER_PAUSED

    def resume(self):
        """Resume the timer"""
        if self.state == TIMER_PAUSED:
            self.state = TIMER_RUNNING
            self.last_update = time.time()

    def is_expired(self):
        """Check if current player's time has expired"""
        times = self.get_current_times()
        return times[self.active_player] <= 0

    def should_sync(self):
        """Check if we should send a full sync to clients"""
        return time.time() - self.last_sync >= TIMER_SYNC_INTERVAL

def start_game_timer(game_id):
    """Start server-authoritative timer for a game"""
    with timer_lock:
        if game_id in game_timers:
            return  # Timer already running

        game_data = games.get(game_id)
        if not game_data:
            return

        # Parse time control
        time_control = game_data.get('time_control', '3+2')
        parts = time_control.split('+')
        minutes = int(parts[0])
        increment = int(parts[1]) if len(parts) > 1 else 0
        base_time = minutes * 60  # Convert to seconds as integer

        # Create Lichess-style timer instance
        timer = LichessStyleTimer(game_id, float(base_time), float(base_time), increment)
        game_timers[game_id] = timer

        # Initialize timer but don't start countdown until first move
        timer.last_update = time.time()
        timer.state = TIMER_PAUSED  # Start paused until first move

        # Update game_data with initial timer values as integers
        game_data['timers'] = {'white': base_time, 'black': base_time}
        game_data['active_timer'] = 'white'
        game_data['timer_started'] = False  # Track if timer has started
        game_data['first_move_start_time'] = time.time()  # When the first move timer started

        print(f"Game {game_id} created - first move countdown starting (20 seconds)")

        # Send immediate timer sync to all players in the room with exact base time

        # Calculate initial piece counts
        piece_counts = calculate_piece_counts(game_data)

        # Send immediate timer sync to all players in the room with exact base time
        socketio.emit('timer_sync', {
            'timers': {'white': base_time, 'black': base_time},
            'active_player': 'white',
            'server_time': time.time(),
            'full_sync': True,
            'game_start': True,
            'timer_paused': True,  # Indicate timer is paused
            'first_move_countdown': True,  # Signal that first move countdown should start
            'piece_counts': piece_counts,
            'countdown_start_time': time.time()  # Fixed countdown reference
        }, room=game_id)

        # Send immediate countdown start signal with server start time
        socketio.emit('first_move_countdown_start', {
            'seconds_left': 20,
            'server_start_time': time.time(),
            'waiting_for': 'white'
        }, room=game_id)

        # Start Lichess-style timer thread with frequent updates

        # Start Lichess-style timer thread with frequent updates
        def timer_thread():
            last_broadcast = time.time()
            last_timeout_check = time.time()

            while True:
                time.sleep(0.1)  # Update every 100ms for smooth countdown

                with timer_lock:
                    if game_id not in game_timers:
                        break

                    timer = game_timers[game_id]
                    game_data = games.get(game_id)

                    if not game_data:
                        print(f"Timer thread stopping for game {game_id}: no game_data")
                        break

                    # Check if game was canceled or finished
                    if game_data.get('status') in ['canceled', 'finished']:
                        print(f"Timer thread stopping for game {game_id}: status = {game_data.get('status')}")
                        break

                    # Check first move timeout - use deadline from game_data (resets for black after white moves)
                    current_time = time.time()
                    if not game_data.get('timer_started', False):
                        # For tournament games, check_first_move_loop handles the timeout logic
                        # This thread only sends countdown updates to clients
                        is_tournament = game_data.get('tournament_id') is not None
                        
                        # Get the deadline and who we're waiting for from game_data
                        first_move_deadline = game_data.get('first_move_deadline', current_time + 20)
                        waiting_for = game_data.get('waiting_for_first_move', 'white')
                        
                        # Calculate remaining time from deadline
                        remaining_time = max(0, first_move_deadline - current_time)

                        # Send countdown update every second
                        if current_time - last_timeout_check >= 1.0:
                            last_timeout_check = current_time

                            # Send countdown update to all players with who we're waiting for
                            socketio.emit('first_move_countdown_update', {
                                'seconds_left': int(remaining_time),
                                'waiting_for': waiting_for
                            }, room=game_id)

                            print(f"First move countdown for {waiting_for}: {int(remaining_time)} seconds remaining")

                        # For non-tournament games, handle timeout here
                        # For tournament games, check_first_move_loop handles it
                        if not is_tournament and remaining_time <= 0:
                            # First move timeout - the player who didn't move loses
                            loser = waiting_for
                            winner = 'black' if loser == 'white' else 'white'
                            
                            game_data['status'] = 'finished'
                            game_data['winner'] = winner
                            game_data['end_reason'] = 'first_move_timeout'
                            
                            loser_username = game_data.get(loser, loser)
                            winner_username = game_data.get(winner, winner)
                            
                            # Update ratings for the win
                            update_ratings(game_data, winner)
                            
                            socketio.emit('game_over', {
                                'winner': winner,
                                'reason': 'first_move_timeout',
                                'message': f'{loser_username} did not make first move within 20 seconds - {winner_username} wins!'
                            }, room=game_id)

                            print(f"Game {game_id}: {loser_username} lost due to first move timeout - {winner_username} wins")
                            break

                    # Skip timer updates if waiting for removal or game not started

                    # Skip timer updates if game not started or if game is finished
                    if not game_data.get('timer_started', False):
                        continue

                    # Stop timer thread immediately if game is finished
                    if game_data.get('status') == 'finished':
                        print(f"Timer thread detected finished game {game_id}, stopping")
                        break

                    # Get current times
                    current_times = timer.get_current_times()

                    # Update game_data timers for compatibility
                    game_data['timers'] = {
                        'white': max(0, int(current_times['white'])),
                        'black': max(0, int(current_times['black']))
                    }

                    # Check for timeout only if timer is actually running and game has started
                    if timer.state == TIMER_RUNNING and timer.is_expired() and timer.move_count > 0:
                        winner = 'black' if timer.active_player == 'white' else 'white'
                        game_data['status'] = 'finished'
                        game_data['winner'] = winner
                        game_data['end_reason'] = 'timeout'

                        # Update ratings
                        update_ratings(game_data, winner)

                        # Get updated user data
                        white_user = users[game_data['white']]
                        black_user = users[game_data['black']]
                        rating_type = get_rating_type(game_data.get('time_control', '3+2'))

                        socketio.emit('game_over', {
                            'winner': winner,
                            'reason': 'timeout',
                            'rating_changes': game_data.get('rating_changes', {}),
                            'new_ratings': {
                                game_data['white']: white_user[f'{rating_type}_rating'],
                                game_data['black']: black_user[f'{rating_type}_rating']
                            },
                            'rating_type': rating_type
                        }, room=game_id)

                        update_tournament_scores(game_data, winner)
                        break

                    # Send timer updates every second (Lichess style)
                    current_time = time.time()
                    if current_time - last_broadcast >= 1.0:
                        # Calculate current piece counts
                        piece_counts = calculate_piece_counts(game_data)

                        update_data = {
                            'timers': {
                                'white': max(0, int(current_times['white'])),
                                'black': max(0, int(current_times['black']))
                            },
                            'active_player': timer.active_player,
                            'server_time': current_time,
                            'is_sync': timer.should_sync(),
                            'piece_counts': piece_counts
                        }

                        if timer.should_sync():
                            timer.last_sync = current_time
                            update_data['full_sync'] = True

                        socketio.emit('timer_update', update_data, room=game_id)
                        last_broadcast = current_time

            # Clean up timer reference
            with timer_lock:
                if game_id in game_timers:
                    del game_timers[game_id]

        timer_thread_obj = threading.Thread(target=timer_thread, daemon=True)
        timer_thread_obj.start()

def stop_game_timer(game_id):
    """Stop server-authoritative timer for a game"""
    with timer_lock:
        if game_id in game_timers:
            timer = game_timers[game_id]
            timer.pause()
            timer.state = TIMER_EXPIRED  # Mark as expired to stop timer thread
            del game_timers[game_id]
            print(f"Timer stopped and cleaned up for game {game_id}")

def switch_player_timer(game_id, new_player):
    """Switch active timer to new player and add increment"""
    with timer_lock:
        if game_id not in game_timers:
            return

        timer = game_timers[game_id]
        game_data = games.get(game_id)
        if not game_data:
            return

        # Get times before switch
        old_times = timer.get_current_times()
        previous_player = timer.active_player

        # Switch player and add increment, get whether increment was applied
        increment_applied = timer.switch_player(new_player)

        # Get times after increment is added
        current_times = timer.get_current_times()

        # Update game_data timers for compatibility
        game_data['timers'] = {
            'white': int(current_times['white']),
            'black': int(current_times['black'])
        }

        # Calculate piece counts for the update
        piece_counts = calculate_piece_counts(game_data)

        # Send immediate timer update with integer values
        socketio.emit('timer_update', {
            'timers': {
                'white': int(current_times['white']),
                'black': int(current_times['black'])
            },
            'active_player': new_player,
            'server_time': time.time(),
            'switch_event': True,
            'increment_applied': increment_applied,
            'previous_player': previous_player,
            'force_sync': True,
            'piece_counts': piece_counts
        }, room=game_id)

        # Also send a full sync immediately after to ensure clients update
        socketio.emit('timer_sync', {
            'timers': {
                'white': int(current_times['white']),
                'black': int(current_times['black'])
            },
            'active_player': new_player,
            'server_time': time.time(),
            'full_sync': True,
            'increment_applied': increment_applied,
            'piece_counts': piece_counts
        }, room=game_id)

def pause_game_timer(game_id):
    """Pause game timer"""
    with timer_lock:
        if game_id in game_timers:
            game_timers[game_id].pause()

def resume_game_timer(game_id):
    """Resume game timer"""
    with timer_lock:
        if game_id in game_timers:
            game_timers[game_id].resume()

def get_timer_info(game_id):
    """Get current timer information"""
    with timer_lock:
        if game_id in game_timers:
            timer = game_timers[game_id]
            return {
                'timers': timer.get_current_times(),
                'active_player': timer.active_player,
                'state': timer.state,
                'server_time': time.time()
            }
    return None

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

class NineMensMorris:
    def __init__(self):
        self.board = [None] * 24  # 24 positions on the board
        self.phase = 1  # 1: placing, 2: moving, 3: flying
        self.current_player = 'white'
        self.white_pieces = 9
        self.black_pieces = 9
        self.total_pieces_placed = 0  # Track total pieces placed on board

        self.moves = []

        # Define mill combinations (lines of 3)
        self.mills = [
            [0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11], [12, 13, 14], [15, 16, 17], [18, 19, 20], [21, 22, 23],
            [0, 9, 21], [3, 10, 18], [6, 11, 15], [1, 4, 7], [16, 19, 22], [8, 12, 17], [5, 13, 20], [2, 14, 23]
        ]

        # Define adjacent positions
        self.adjacents = {
            0: [1, 9], 1: [0, 2, 4], 2: [1, 14], 3: [4, 10], 4: [1, 3, 5, 7], 5: [4, 13],
            6: [7, 11], 7: [4, 6, 8], 8: [7, 12], 9: [0, 10, 21], 10: [3, 9, 11, 18],
            11: [6, 10, 15], 12: [8, 13, 17], 13: [5, 12, 14, 20], 14: [2, 13, 23],
            15: [11, 16], 16: [15, 17, 19], 17: [12, 16], 18: [10, 19], 19: [16, 18, 20, 22],
            20: [13, 19], 21: [9, 22], 22: [19, 21, 23], 23: [14, 22]
        }

    def is_mill(self, position):
        player = self.board[position]
        if not player:
            return False

        for mill in self.mills:
            if position in mill:
                if all(self.board[pos] == player for pos in mill):
                    return True
        return False

    def can_remove(self, position):
        if not self.board[position] or self.board[position] == self.current_player:
            return False

        # Never allow removal of pieces in mills
        return not self.is_mill(position)

    def make_move(self, from_pos, to_pos, remove_pos=None):
        if self.phase == 1:  # Placing phase
            if self.board[to_pos] is not None:
                return False

            self.board[to_pos] = self.current_player
            if self.current_player == 'white':
                self.white_pieces -= 1
            else:
                self.black_pieces -= 1

            # Increment total pieces placed counter
            self.total_pieces_placed += 1

            mill_formed = self.is_mill(to_pos)

            # Check if we should move to phase 2 (18 total pieces placed)
            if self.total_pieces_placed >= 18:
                self.phase = 2

        elif self.phase == 2:  # Moving phase
            if self.board[from_pos] != self.current_player or self.board[to_pos] is not None:
                return False

            # Check if current player has exactly 3 pieces (can fly)
            current_player_pieces = sum(1 for piece in self.board if piece == self.current_player)

            if current_player_pieces == 3:
                # Player can fly to any empty position
                pass  # No adjacency restriction
            else:
                # Player must move to adjacent position
                if to_pos not in self.adjacents[from_pos]:
                    return False

            self.board[from_pos] = None
            self.board[to_pos] = self.current_player
            mill_formed = self.is_mill(to_pos)

            # Check if any player has only 3 pieces left (transition to flying phase)
            white_pieces = sum(1 for piece in self.board if piece == 'white')
            black_pieces = sum(1 for piece in self.board if piece == 'black')
            if white_pieces == 3 or black_pieces == 3:
                self.phase = 3

        elif self.phase == 3:  # Flying phase
            if self.board[from_pos] != self.current_player or self.board[to_pos] is not None:
                return False

            # Check if current player still has exactly 3 pieces (can fly)
            current_player_pieces = sum(1 for piece in self.board if piece == self.current_player)

            if current_player_pieces == 3:
                # Player can fly to any empty position
                pass  # No adjacency restriction
            else:
                # Player with more than 3 pieces must move adjacently
                if to_pos not in self.adjacents[from_pos]:
                    return False

            self.board[from_pos] = None
            self.board[to_pos] = self.current_player
            mill_formed = self.is_mill(to_pos)

        # Handle piece removal after mill
        piece_removed = False
        if mill_formed and remove_pos is not None:
            if self.can_remove(remove_pos):
                self.board[remove_pos] = None
                piece_removed = True

        # Check if any pieces can be removed when mill is formed
        can_remove_any = False
        if mill_formed:
            opponent_color = 'black' if self.current_player == 'white' else 'white'
            opponent_pieces = [i for i, piece in enumerate(self.board) if piece == opponent_color]
            can_remove_any = any(self.can_remove(pos) for pos in opponent_pieces)

        self.moves.append({
            'from': from_pos,
            'to': to_pos,
            'remove': remove_pos if piece_removed else None,
            'player': self.current_player,
            'mill': mill_formed
        })

        # Switch player logic:
        # - If no mill was formed, switch player
        # - If mill was formed and piece was removed, switch player
        # - If mill was formed but no pieces can be removed, switch player (continue game)
        # - If mill was formed and pieces can be removed but none was selected yet, don't switch
        if not mill_formed or (mill_formed and piece_removed) or (mill_formed and not can_remove_any):
            self.current_player = 'black' if self.current_player == 'white' else 'white'

        return {'success': True, 'mill_formed': mill_formed, 'waiting_for_removal': mill_formed and remove_pos is None and can_remove_any}

    def get_winner(self):
        white_pieces = sum(1 for piece in self.board if piece == 'white')
        black_pieces = sum(1 for piece in self.board if piece == 'black')

        # Win condition: opponent has 2 or fewer pieces after placing phase
        if self.phase >= 2:  # Only check after placing phase
            if white_pieces <= 2:
                return 'black'
            if black_pieces <= 2:
                return 'white'

        # Check for draw after 50 moves without mill or capture
        if len(self.moves) >= 50:
            recent_moves = self.moves[-50:]
            if not any(move.get('mill', False) or move.get('remove') for move in recent_moves):
                return 'draw'

        return None

@app.route('/health')
def health_check():
    return 'OK', 200

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'favicon.ico', mimetype='image/x-icon')

@app.route('/robots.txt')
def robots_txt():
    base_url = request.host_url.rstrip('/')
    content = f"""User-agent: *
Allow: /
Sitemap: {base_url}/sitemap.xml
"""
    return content, 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def sitemap_xml():
    base_url = request.host_url.rstrip('/')
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{base_url}/lobby</loc>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>{base_url}/tournaments</loc>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>{base_url}/leaderboard</loc>
    <changefreq>daily</changefreq>
    <priority>0.7</priority>
  </url>
</urlset>"""
    return content, 200, {'Content-Type': 'application/xml'}

@app.route('/')
def home():
    if not app_ready:
        return 'OK', 200
    if 'username' in session:
        username = session['username']
        
        # Check if logged in user is banned
        if username in banned_users:
            session.pop('username', None)
            flash('Your account has been permanently banned.')
            return render_template('index.html')
            
        user = users.get(username)
        if user:
            # Get best current game
            best_game = get_best_live_game()
            return render_template('lobby.html', user=user, best_game=best_game)
        else:
            # Clear invalid session
            session.pop('username', None)
    return render_template('index.html')

def get_best_live_game():
    """Get the highest rated live game"""
    best_game = None
    highest_avg_rating = 0

    for game_id, game_data in games.items():
        if game_data.get('status') == 'playing':
            white_user = users.get(game_data['white'])
            black_user = users.get(game_data['black'])

            if white_user and black_user:
                rating_type = get_rating_type(game_data.get('time_control', '3+2'))
                white_rating = white_user.get(f'{rating_type}_rating', 1200)
                black_rating = black_user.get(f'{rating_type}_rating', 120)
                avg_rating = (white_rating + black_rating) / 2

                if avg_rating > highest_avg_rating:
                    highest_avg_rating = avg_rating
                    best_game = {
                        'id': game_id,
                        'white': game_data['white'],
                        'black': game_data['black'],
                        'white_rating': white_rating,
                        'black_rating': black_rating,
                        'time_control': game_data.get('time_control', '3+2')
                    }

    return best_game

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        enable_2fa = request.form.get('enable_2fa') == 'on'
        email = request.form.get('email', '').strip()

        if ' ' in username:
            flash('Username cannot contain spaces!')
            return render_template('signup.html')

        if len(username) > 15:
            flash('Username cannot be longer than 15 characters!')
            return render_template('signup.html')

        if username in users:
            flash('Korisničko ime već postoji!')
            return render_template('signup.html')
        
        if enable_2fa and not email:
            flash('Email is required when enabling 2FA protection!')
            return render_template('signup.html')

        users[username] = {
            'username': username,
            'password': hash_password(password),
            'bullet_rating': 100,
            'blitz_rating': 100,
            'games_played': {'bullet': 0, 'blitz': 0},
            'wins': {'bullet': 0, 'blitz': 0},
            'losses': {'bullet': 0, 'blitz': 0},
            'draws': {'bullet': 0, 'blitz': 0},
            'created': datetime.now().isoformat(),
            'color': '#c9c9c9',
            'is_admin': False,
            'best_wins': {'bullet': [], 'blitz': []},
            'tournaments_won': {'daily': 0, 'weekly': 0, 'monthly': 0, 'marathon': 0, 'world_cup': 0},
            'trophies': [],
            'elo_history': {'bullet': [], 'blitz': []},
            'email': email if enable_2fa else '',
            '2fa_enabled': enable_2fa
        }
        
        # Save new user to database
        save_user_to_db(username)

        session.permanent = True
        session['username'] = username
        return redirect(url_for('home'))

    return render_template('signup.html')

def generate_2fa_code():
    """Generate a 6-digit verification code"""
    return ''.join([str(random.randint(0, 9)) for _ in range(6)])

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # Check if user is banned first
        if username in banned_users:
            flash('This account has been permanently banned and cannot log in.')
            return render_template('login.html')

        user = users.get(username)
        if user and user['password'] == hash_password(password):
            # Check if 2FA is enabled
            if user.get('2fa_enabled') and user.get('email'):
                # Generate and store 2FA code
                code = generate_2fa_code()
                pending_2fa_codes[username] = {
                    'code': code,
                    'expires': time.time() + 600  # 10 minutes
                }
                # In production, you would send this via email
                # For now, store in session for demo purposes
                session['pending_2fa_user'] = username
                flash(f'A 6-digit verification code has been sent to your email. (Demo code: {code})')
                return redirect(url_for('verify_2fa'))
            
            session.permanent = True
            session['username'] = username
            return redirect(url_for('home'))

        flash('Neispravno korisničko ime ili lozinka!')

    return render_template('login.html')

@app.route('/verify-2fa', methods=['GET', 'POST'])
def verify_2fa():
    pending_user = session.get('pending_2fa_user')
    if not pending_user:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        entered_code = request.form.get('code', '').strip()
        pending = pending_2fa_codes.get(pending_user)
        
        if pending and time.time() < pending['expires']:
            if entered_code == pending['code']:
                # Success - log them in
                del pending_2fa_codes[pending_user]
                session.pop('pending_2fa_user', None)
                session.permanent = True
                session['username'] = pending_user
                return redirect(url_for('home'))
            else:
                flash('Invalid verification code!')
        else:
            flash('Verification code expired. Please log in again.')
            session.pop('pending_2fa_user', None)
            return redirect(url_for('login'))
    
    return render_template('verify_2fa.html', username=pending_user)

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('home'))

@app.route('/profile/<username>')
def profile(username):
    user = users.get(username)
    if not user:
        return "Korisnik nije pronađen", 404

    is_banned = username in banned_users
    
    # Use the higher of bullet or blitz rating for title
    max_rating = max(user.get('bullet_rating', 100), user.get('blitz_rating', 100))
    title, color = get_title(max_rating, user)
    
    # Get ranking badges for this user
    ranking_badges = get_ranking_badge(username)
    
    # Check if viewing own profile
    is_own_profile = session.get('username') == username
    
    return render_template('profile.html', user=user, title=title, color=color, ranking_badges=ranking_badges, is_own_profile=is_own_profile, is_banned=is_banned)

@app.route('/profile/<username>/security', methods=['GET', 'POST'])
def profile_security(username):
    if 'username' not in session or session['username'] != username:
        return redirect(url_for('login'))
    
    user = users.get(username)
    if not user:
        return redirect(url_for('home'))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'enable_2fa':
            email = request.form.get('email', '').strip()
            if not email:
                flash('Email is required to enable 2FA!')
            else:
                user['email'] = email
                user['2fa_enabled'] = True
                save_user_to_db(username)
                flash('Email protection enabled! You will receive a verification code when logging in.')
        
        elif action == 'disable_2fa':
            user['2fa_enabled'] = False
            save_user_to_db(username)
            flash('Email protection has been disabled.')
        
        elif action == 'update_email':
            email = request.form.get('email', '').strip()
            if email:
                user['email'] = email
                save_user_to_db(username)
                flash('Email updated successfully!')
        
        return redirect(url_for('profile_security', username=username))
    
    return render_template('profile_security.html', user=user)

@app.route('/friends')
def friends_page():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    username = session['username']
    
    # Get accepted friends from database
    with app.app_context():
        user_friends = []
        pending_requests = []
        
        # Get all accepted friendships
        accepted = Friendship.query.filter(
            ((Friendship.user1 == username) | (Friendship.user2 == username)) &
            (Friendship.status == 'accepted')
        ).all()
        
        for f in accepted:
            friend_name = f.user2 if f.user1 == username else f.user1
            friend_data = users.get(friend_name, {})
            if friend_data:
                user_friends.append({
                    'username': friend_name,
                    'rating': max(friend_data.get('bullet_rating', 100), friend_data.get('blitz_rating', 100)),
                    'online': friend_name in [u for u in online_users.values()]
                })
        
        # Get pending friend requests (where user is receiver)
        pending = Friendship.query.filter(
            (Friendship.user2 == username) &
            (Friendship.status == 'pending')
        ).all()
        
        for f in pending:
            sender_data = users.get(f.user1, {})
            if sender_data:
                pending_requests.append({
                    'id': f.id,
                    'username': f.user1,
                    'rating': max(sender_data.get('bullet_rating', 100), sender_data.get('blitz_rating', 100)),
                    'created': f.created
                })
    
    return render_template('friends.html', friends=user_friends, requests=pending_requests, users=users, get_title=get_title)

@app.route('/api/friend/request', methods=['POST'])
def send_friend_request():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.get_json()
    target_user = data.get('username')
    sender = session['username']
    
    if not target_user or target_user not in users:
        return jsonify({'error': 'User not found'}), 404
    
    if target_user == sender:
        return jsonify({'error': 'Cannot add yourself'}), 400
    
    with app.app_context():
        # Check if friendship already exists
        existing = Friendship.query.filter(
            ((Friendship.user1 == sender) & (Friendship.user2 == target_user)) |
            ((Friendship.user1 == target_user) & (Friendship.user2 == sender))
        ).first()
        
        if existing:
            if existing.status == 'accepted':
                return jsonify({'error': 'Already friends'}), 400
            else:
                return jsonify({'error': 'Request already pending'}), 400
        
        # Create new friend request
        new_request = Friendship(user1=sender, user2=target_user, status='pending')
        db.session.add(new_request)
        db.session.commit()
        
        # Notify the target user via socket
        target_sid = None
        for sid, uname in online_users.items():
            if uname == target_user:
                target_sid = sid
                break
        
        if target_sid:
            socketio.emit('friend_request', {
                'from_user': sender,
                'message': f'{sender} sent you a friend request!'
            }, room=target_sid)
    
    return jsonify({'success': True, 'message': 'Friend request sent!'})

@app.route('/api/friend/accept', methods=['POST'])
def accept_friend_request():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.get_json()
    request_id = data.get('request_id')
    username = session['username']
    
    with app.app_context():
        friendship = Friendship.query.get(request_id)
        
        if not friendship:
            return jsonify({'error': 'Request not found'}), 404
        
        if friendship.user2 != username:
            return jsonify({'error': 'Not authorized'}), 403
        
        friendship.status = 'accepted'
        db.session.commit()
        
        # Notify the sender
        sender_sid = None
        for sid, uname in online_users.items():
            if uname == friendship.user1:
                sender_sid = sid
                break
        
        if sender_sid:
            socketio.emit('friend_accepted', {
                'by_user': username,
                'message': f'{username} accepted your friend request!'
            }, room=sender_sid)
    
    return jsonify({'success': True, 'message': 'Friend request accepted!'})

@app.route('/api/friend/reject', methods=['POST'])
def reject_friend_request():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.get_json()
    request_id = data.get('request_id')
    username = session['username']
    
    with app.app_context():
        friendship = Friendship.query.get(request_id)
        
        if not friendship:
            return jsonify({'error': 'Request not found'}), 404
        
        if friendship.user2 != username:
            return jsonify({'error': 'Not authorized'}), 403
        
        db.session.delete(friendship)
        db.session.commit()
    
    return jsonify({'success': True, 'message': 'Friend request rejected'})

@app.route('/api/friend/remove', methods=['POST'])
def remove_friend():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.get_json()
    target_user = data.get('username')
    username = session['username']

    if not target_user:
        return jsonify({'error': 'No user specified'}), 400

    with app.app_context():
        friendship = Friendship.query.filter(
            ((Friendship.user1 == username) & (Friendship.user2 == target_user)) |
            ((Friendship.user1 == target_user) & (Friendship.user2 == username))
        ).filter(Friendship.status == 'accepted').first()

        if not friendship:
            return jsonify({'error': 'Not friends with this user'}), 404

        db.session.delete(friendship)
        db.session.commit()

    return jsonify({'success': True, 'message': f'Removed {target_user} from friends'})

@app.route('/api/friend/status/<target_user>')
def get_friend_status(target_user):
    if 'username' not in session:
        return jsonify({'status': 'not_logged_in'})
    
    username = session['username']
    
    if target_user == username:
        return jsonify({'status': 'self'})
    
    with app.app_context():
        friendship = Friendship.query.filter(
            ((Friendship.user1 == username) & (Friendship.user2 == target_user)) |
            ((Friendship.user1 == target_user) & (Friendship.user2 == username))
        ).first()
        
        if not friendship:
            return jsonify({'status': 'none'})
        
        if friendship.status == 'accepted':
            return jsonify({'status': 'friends'})
        
        # Pending - check who sent
        if friendship.user1 == username:
            return jsonify({'status': 'pending_sent'})
        else:
            return jsonify({'status': 'pending_received', 'request_id': friendship.id})
    
    return jsonify({'status': 'none'})

@app.route('/api/messages/<friend_username>')
def get_private_messages(friend_username):
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    
    with app.app_context():
        # Verify they are friends
        friendship = Friendship.query.filter(
            ((Friendship.user1 == username) & (Friendship.user2 == friend_username)) |
            ((Friendship.user1 == friend_username) & (Friendship.user2 == username))
        ).filter(Friendship.status == 'accepted').first()
        
        if not friendship:
            return jsonify({'error': 'Not friends'}), 403
        
        # Get messages between users
        messages = PrivateMessage.query.filter(
            ((PrivateMessage.sender == username) & (PrivateMessage.receiver == friend_username)) |
            ((PrivateMessage.sender == friend_username) & (PrivateMessage.receiver == username))
        ).order_by(PrivateMessage.id).all()
        
        # Mark received messages as read
        PrivateMessage.query.filter(
            (PrivateMessage.sender == friend_username) &
            (PrivateMessage.receiver == username) &
            (PrivateMessage.read == False)
        ).update({'read': True})
        db.session.commit()
        
        return jsonify({'messages': [m.to_dict() for m in messages]})

@app.route('/api/messages/send', methods=['POST'])
def send_private_message():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.get_json()
    receiver = data.get('receiver')
    message = data.get('message', '').strip()
    sender = session['username']
    
    if not message or not receiver:
        return jsonify({'error': 'Invalid message'}), 400
    
    with app.app_context():
        # Verify they are friends
        friendship = Friendship.query.filter(
            ((Friendship.user1 == sender) & (Friendship.user2 == receiver)) |
            ((Friendship.user1 == receiver) & (Friendship.user2 == sender))
        ).filter(Friendship.status == 'accepted').first()
        
        if not friendship:
            return jsonify({'error': 'Not friends'}), 403
        
        # Save message
        new_message = PrivateMessage(sender=sender, receiver=receiver, message=message)
        db.session.add(new_message)
        db.session.commit()
        
        # Notify receiver via socket
        receiver_sid = None
        for sid, uname in online_users.items():
            if uname == receiver:
                receiver_sid = sid
                break
        
        if receiver_sid:
            socketio.emit('private_message', {
                'from_user': sender,
                'message': message,
                'timestamp': new_message.timestamp
            }, room=receiver_sid)
    
    return jsonify({'success': True, 'message': new_message.to_dict()})

@app.route('/api/unread-messages')
def get_unread_messages_count():
    if 'username' not in session:
        return jsonify({'count': 0, 'senders': []})
    
    username = session['username']
    
    with app.app_context():
        unread = PrivateMessage.query.filter(
            (PrivateMessage.receiver == username) &
            (PrivateMessage.read == False)
        ).all()
        
        senders = {}
        for msg in unread:
            if msg.sender not in senders:
                senders[msg.sender] = 0
            senders[msg.sender] += 1
        
        sender_list = [{'username': s, 'count': c} for s, c in senders.items()]
        
        return jsonify({'count': len(unread), 'senders': sender_list})

@app.route('/api/pending-friend-requests')
def get_pending_friend_requests_count():
    if 'username' not in session:
        return jsonify({'count': 0, 'from_users': []})
    
    username = session['username']
    
    with app.app_context():
        pending = Friendship.query.filter(
            (Friendship.user2 == username) &
            (Friendship.status == 'pending')
        ).all()
        
        from_users = [f.user1 for f in pending]
        
        return jsonify({'count': len(pending), 'from_users': from_users})

@app.route('/api/mark-notifications-read', methods=['POST'])
def mark_notifications_read():
    if 'username' not in session:
        return jsonify({'success': False})
    
    username = session['username']
    
    with app.app_context():
        try:
            PrivateMessage.query.filter(
                (PrivateMessage.receiver == username) &
                (PrivateMessage.read == False)
            ).update({PrivateMessage.read: True})
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Failed to mark notifications read: {e}")
    
    return jsonify({'success': True})

@app.route('/play')
def play():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('play.html', users=users, get_title=get_title, spectate_mode=False, spectate_game_id=None, piece_designs=PIECE_DESIGNS)

@app.route('/play/<game_id>')
def play_game(game_id):
    if 'username' not in session:
        return redirect(url_for('login'))
    
    # Check if spectating
    spectate = request.args.get('spectate', 'false').lower() == 'true'
    
    # Check if game exists
    if game_id not in games:
        flash('Game not found')
        return redirect(url_for('play'))
    
    game_data = games[game_id]
    
    # If spectating, pass spectate mode to template
    if spectate:
        return render_template('play.html', users=users, get_title=get_title, 
                             spectate_mode=True, spectate_game_id=game_id, piece_designs=PIECE_DESIGNS)
    
    # Otherwise, normal play mode - join specific game
    return render_template('play.html', users=users, get_title=get_title, 
                         spectate_mode=False, spectate_game_id=game_id, piece_designs=PIECE_DESIGNS)

@app.route('/tournaments')
def tournaments_page():
    if 'username' not in session:
        return redirect(url_for('login'))

    # Create scheduled tournaments and start any that should be active
    create_scheduled_tournaments()
    start_scheduled_tournaments()

    # Get all tournaments (scheduled, active, and recently finished) for the next week
    current_time = datetime.now()
    week_from_now = current_time + timedelta(days=7)

    admin_tournaments = []
    upcoming_tournaments = []
    world_cup_tournaments = []
    
    for t in tournaments.values():
        start_time = datetime.fromisoformat(t['start_time'])
        
        # World Cups are always included (shown at bottom)
        if t.get('tournament_type') == 'world_cup' or t.get('is_world_cup'):
            world_cup_tournaments.append(t)
            continue
        
        # Admin tournaments go to top
        if t.get('admin_only'):
            admin_tournaments.append(t)
            continue
            
        # Include scheduled and active tournaments, plus finished tournaments (they'll be removed after 5 min)
        if (current_time <= start_time <= week_from_now or 
            t['status'] == 'active' or 
            t['status'] == 'finished'):
            upcoming_tournaments.append(t)

    # Sort admin tournaments by start time (active ones first)
    admin_tournaments.sort(key=lambda x: (0 if x['status'] == 'active' else 1, x['start_time']))
    
    # Sort regular tournaments by start time
    upcoming_tournaments.sort(key=lambda x: x['start_time'])
    
    # Add all World Cups at the end (always visible), sorted by start time
    world_cup_tournaments.sort(key=lambda x: x['start_time'])
    
    # Final list: admin tournaments first, then regular, then world cups
    all_tournaments = admin_tournaments + upcoming_tournaments + world_cup_tournaments

    # Pass server time so client can accurately calculate time differences
    return render_template('tournaments.html', tournaments=all_tournaments, server_now=current_time.isoformat())

@app.route('/games/<username>')
def user_games(username):
    if 'username' not in session:
        return redirect(url_for('login'))

    user = users.get(username)
    if not user:
        return "User not found", 404

    return render_template('games.html', username=username, user=user)

@app.route('/analysis/<game_id>')
def analysis(game_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    game = games.get(game_id)
    if not game:
        flash('This game is no longer available. It may have been played before game history was saved.')
        return redirect(url_for('home'))

    # Only allow analysis of finished games
    if game.get('status') != 'finished':
        flash('You can only analyze finished games!')
        return redirect(url_for('home'))
    
    # Check if coming from tournament (for resume button)
    tournament_id = request.args.get('tournament_id')

    # Get ranking badges for both players
    white_badges = get_ranking_badge(game.get('white'))
    black_badges = get_ranking_badge(game.get('black'))
    
    # Prepare game data for analysis template
    game_data = {
        'id': game_id,
        'white': game.get('white'),
        'black': game.get('black'),
        'winner': game.get('winner'),
        'end_reason': game.get('end_reason', 'normal'),
        'start_time': game.get('start_time'),
        'end_time': game.get('end_time'),
        'time_control': game.get('time_control', '3+2'),
        'status': game.get('status'),
        'moves': game.get('moves', []),
        'positions': game.get('positions', []),
        'rating_changes': game.get('rating_changes', {}),
        'move_count': len(game.get('moves', [])),
        'detailed_moves': [],
        'white_user_data': users.get(game.get('white'), {}),
        'black_user_data': users.get(game.get('black'), {}),
        'white_rating': 0,
        'black_rating': 0,
        'white_badges': white_badges,
        'black_badges': black_badges
    }

    # Get ratings
    rating_type = get_rating_type(game.get('time_control', '3+2'))
    if game_data['white_user_data']:
        game_data['white_rating'] = game_data['white_user_data'].get(f'{rating_type}_rating', 100)
    if game_data['black_user_data']:
        game_data['black_rating'] = game_data['black_user_data'].get(f'{rating_type}_rating', 100)

    # Determine result from current user's perspective for display
    current_user = session.get('username', '')
    winner = game_data.get('winner')

    # Check if current user is white or black to determine if they won/lost
    user_is_white = current_user == game_data.get('white')
    user_is_black = current_user == game_data.get('black')

    if winner == 'draw':
        game_data['user_result'] = 'Draw'
        game_data['user_result_class'] = 'draw'
    elif (winner == 'white' and user_is_white) or (winner == 'black' and user_is_black):
        game_data['user_result'] = 'Win'
        game_data['user_result_class'] = 'win'
    elif (winner == 'white' and user_is_black) or (winner == 'black' and user_is_white):
        game_data['user_result'] = 'Loss'
        game_data['user_result_class'] = 'loss'
    else:
        game_data['user_result'] = 'Unknown'
        game_data['user_result_class'] = 'unknown'

    # Create detailed move data for analysis
    if game_data['moves']:
        for i, move in enumerate(game_data['moves']):
            # Get timer info from move if available
            timers = move.get('timers', {'white': 180, 'black': 180})

            detailed_move = {
                'move_number': i + 1,
                'player': move.get('player', 'white'),
                'from': move.get('from'),
                'to': move.get('to'),
                'remove': move.get('remove'),
                'mill': move.get('mill', False),
                'phase': 1 if i < 18 else (2 if any(sum(1 for p in pos if p) > 6 for pos in game_data['positions'][i:i+2]) else 3),
                'white_time_before': timers['white'],
                'black_time_before': timers['black']
            }
            game_data['detailed_moves'].append(detailed_move)

    # Simple analysis - evaluate each position
    analysis_data = analyze_game(game)
    return render_template('analysis.html', game=game_data, analysis=analysis_data, tournament_id=tournament_id)

@app.route('/tournament/<tournament_id>/results')
def tournament_results(tournament_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    tournament = tournaments.get(tournament_id)
    if not tournament:
        return "Tournament not found", 404

    # Pass user data for player colors and ratings
    users_data = {username: users.get(username, {}) for username in tournament.get('players', {}).keys()}
    
    # Sort players by score (descending) for the template
    sorted_players = sorted(
        tournament.get('players', {}).items(),
        key=lambda x: (x[1].get('score', 0), len(x[1].get('wins', []))),
        reverse=True
    )

    return render_template('tournament_results.html', tournament=tournament, users_data=users_data, sorted_players=sorted_players)

@app.route('/tournament/<tournament_id>/leaderboard')
def tournament_leaderboard(tournament_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    tournament = tournaments.get(tournament_id)
    is_archived = False
    
    # Check archived tournaments if not found in active ones
    if not tournament:
        tournament = archived_tournaments.get(tournament_id)
        is_archived = True
        
    if not tournament:
        return "Tournament not found", 404

    # Determine rating type based on time control (bullet if base time <= 2 min, else blitz)
    time_control = tournament.get('time_control', '3+2')
    base_time = int(time_control.split('+')[0]) if '+' in time_control else 3
    rating_type = 'bullet_rating' if base_time <= 2 else 'blitz_rating'
    
    # Sort players by score (descending), then by ELO rating (descending) for tie-breaking
    def get_player_sort_key(item):
        username, data = item
        score = data.get('score', 0)
        # Get player's rating from users dict
        player_rating = users.get(username, {}).get(rating_type, 1500)
        return (-score, -player_rating)  # Negative for descending order
    
    sorted_players = sorted(tournament['players'].items(), key=get_player_sort_key)

    # Pass server time so client can accurately calculate time differences
    return render_template('tournament_leaderboard.html', tournament=tournament, players=sorted_players, users=users, get_title=get_title, paused_users=paused_users, server_now=datetime.now().isoformat(), is_archived=is_archived, rating_type=rating_type)

def analyze_game(game):
    """Simple game analysis"""
    analysis = []
    position_values = []

    # Ensure we have both moves and positions
    moves = game.get('moves', [])
    positions = game.get('positions', [])

    # If no moves, return empty analysis
    if not moves:
        return []

    # If positions is empty or shorter than moves, create dummy positions
    if not positions or len(positions) <= len(moves):
        # Create a simple empty board position for each move
        empty_board = [None] * 24
        positions = [empty_board[:] for _ in range(len(moves) + 1)]

    for i, move in enumerate(moves):
        # Use the position after the move (i+1) if available, otherwise use current position
        position_index = min(i + 1, len(positions) - 1)
        current_position = positions[position_index]

        # Simple evaluation based on piece count and mills
        white_pieces = sum(1 for pce in current_position if pce == 'white')
        black_pieces = sum(1 for pce in current_position if pce == 'black')

        # Basic evaluation
        eval_score = (white_pieces - black_pieces) * 0.3

        if move.get('mill'):
            eval_score += 0.5 if move['player'] == 'white' else -0.5

        position_values.append(eval_score)

        analysis.append({
            'move_number': i + 1,
            'move': move,
            'evaluation': eval_score,
            'comment': get_move_comment(move, eval_score)
        })

    return analysis

def get_move_comment(move, eval_score):
    if move.get('mill'):
        return "Odličan potez! Formiran je mlin."
    elif abs(eval_score) > 1.0:
        return "Jak potez" if eval_score > 0 else "Slab potez"
    else:
        return "Standardan potez"

def create_scheduled_tournaments():
    """Create scheduled tournaments for the next week/month/year"""
    current_time = datetime.now()
    
    # Time controls for each category
    TIME_CONTROLS = ['1+0', '3+2', '5+0']
    
    # Always ensure there's at least one active tournament running
    active_tournaments = [t for t in tournaments.values() if t.get('status') == 'active']
    if not active_tournaments:
        # Create an immediate active tournament
        tournament_id = str(uuid.uuid4())
        config = TOURNAMENT_TYPES['daily']
        time_control = random.choice(TIME_CONTROLS)
        start_time = current_time - timedelta(minutes=5)  # Started 5 min ago
        end_time = start_time + timedelta(minutes=60)  # Lasts 1 hour
        
        tournaments[tournament_id] = {
            'id': tournament_id,
            'name': f"Daily Arena {time_control}",
            'tournament_type': 'daily',
            'time_control': time_control,
            'duration': 60,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'status': 'active',
            'players': {},
            'games': [],
            'color': config['color'],
            'leaderboard': [],
            'prizes': {}
        }

    # Create tournaments for the next 7 days
    for days_ahead in range(7):
        future_date = current_time + timedelta(days=days_ahead)

        # Daily Arena - every hour at 3 minutes past the hour
        for hour in range(24):
            start_time = future_date.replace(hour=hour, minute=3, second=0, microsecond=0)
            if start_time > current_time:  # Only future tournaments
                create_tournament_if_not_exists('daily', start_time)

    # Weekly Arenas - 3 per week, one for each time control
    # Spread across Mon/Wed/Fri at 18:00 (evening time)
    weekly_schedule = [
        (0, 18, '1+0'),   # Monday 18:00 - Bullet
        (2, 19, '3+2'),   # Wednesday 19:00 - Blitz
        (4, 20, '5+0'),   # Friday 20:00 - Rapid
    ]
    for days_ahead in range(14):  # Look 2 weeks ahead
        future_date = current_time + timedelta(days=days_ahead)
        for weekday, hour, time_control in weekly_schedule:
            if future_date.weekday() == weekday:
                start_time = future_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                if start_time > current_time:
                    create_tournament_if_not_exists_with_tc('weekly', start_time, time_control)

    # Monthly Arenas - 3 per month, completely random dates, times, and time controls
    # Create for current month and next month
    for month_offset in range(2):
        month_date = current_time + timedelta(days=30 * month_offset)
        year = month_date.year
        month = month_date.month
        
        # Get days in this month
        if month == 12:
            days_in_month = 31
        else:
            next_month = datetime(year, month + 1, 1) if month < 12 else datetime(year + 1, 1, 1)
            days_in_month = (next_month - datetime(year, month, 1)).days
        
        # Use deterministic random based on year/month for consistency
        random.seed(year * 100 + month + 7777)  # Different seed for variety
        random_days = random.sample(range(1, min(days_in_month + 1, 29)), 3)  # Pick 3 random days
        random_hours = [random.randint(10, 22) for _ in range(3)]  # Random hours 10:00-22:00
        random_minutes = [random.choice([0, 15, 30, 45]) for _ in range(3)]  # Random minutes
        random_time_controls = [random.choice(TIME_CONTROLS) for _ in range(3)]  # Random time controls
        random.seed()  # Reset random seed
        
        for i in range(3):
            try:
                start_time = datetime(year, month, random_days[i], random_hours[i], random_minutes[i], 0)
                if start_time > current_time:
                    create_tournament_if_not_exists_with_tc('monthly', start_time, random_time_controls[i])
            except ValueError:
                pass  # Skip invalid dates

    # Marathon Arena - Every 3 months on 1st at 12:00
    for days_ahead in range(120):  # Look 4 months ahead
        future_date = current_time + timedelta(days=days_ahead)
        if future_date.day == 1 and future_date.month % 3 == 1:
            start_time = future_date.replace(hour=12, minute=0, second=0, microsecond=0)
            if start_time > current_time:
                create_tournament_if_not_exists('marathon', start_time)

    # World Cup - Once per year, always visible
    # Find the next World Cup date (December 31st at 12:00)
    create_annual_world_cup(current_time)

def create_tournament_if_not_exists(tournament_type, start_time):
    """Create tournament if it doesn't already exist"""
    # Check if tournament already exists for this exact time
    existing = any(
        t.get('tournament_type') == tournament_type and 
        abs((datetime.fromisoformat(t['start_time']) - start_time).total_seconds()) < 300  # Within 5 minutes
        for t in tournaments.values()
    )

    if not existing:
        tournament_id = str(uuid.uuid4())
        config = TOURNAMENT_TYPES[tournament_type]

        # Random time control selection for all tournaments
        time_controls = ['1+0', '3+2', '5+0']
        time_control = random.choice(time_controls)
        
        if tournament_type == 'world_cup':
            tournament_name = f"Mill World Cup {time_control}"
        elif tournament_type == 'marathon':
            tournament_name = f"Marathon Arena {time_control}"
        else:
            tournament_name = f"{config['name']} {time_control}"

        tournaments[tournament_id] = {
            'id': tournament_id,
            'name': tournament_name,
            'tournament_type': tournament_type,
            'time_control': time_control,
            'duration': config['duration'],
            'start_time': start_time.isoformat(),
            'end_time': (start_time + timedelta(minutes=config['duration'])).isoformat(),
            'status': 'scheduled',  # New status for future tournaments
            'players': {},
            'games': [],
            'color': config['color'],
            'leaderboard': [],
            'prizes': get_tournament_prizes(tournament_type)
        }

def create_tournament_if_not_exists_with_tc(tournament_type, start_time, time_control):
    """Create tournament with specific time control if it doesn't already exist"""
    # Check if tournament already exists for this exact time and time control
    existing = any(
        t.get('tournament_type') == tournament_type and 
        t.get('time_control') == time_control and
        abs((datetime.fromisoformat(t['start_time']) - start_time).total_seconds()) < 300
        for t in tournaments.values()
    )

    if not existing:
        tournament_id = str(uuid.uuid4())
        config = TOURNAMENT_TYPES[tournament_type]
        
        tournament_name = f"{config['name']} {time_control}"

        tournaments[tournament_id] = {
            'id': tournament_id,
            'name': tournament_name,
            'tournament_type': tournament_type,
            'time_control': time_control,
            'duration': config['duration'],
            'start_time': start_time.isoformat(),
            'end_time': (start_time + timedelta(minutes=config['duration'])).isoformat(),
            'status': 'scheduled',
            'players': {},
            'games': [],
            'color': config['color'],
            'leaderboard': [],
            'prizes': get_tournament_prizes(tournament_type)
        }

def create_annual_world_cup(current_time):
    """Create 3 annual World Cup tournaments - one for each time control on different dates"""
    year = current_time.year
    
    # 3 World Cups per year on different dates and times
    # Bullet (1+0): April 15 at 16:00
    # Blitz (3+2): August 20 at 18:00  
    # Rapid (5+0): December 31 at 14:00
    world_cup_schedule = [
        {'time_control': '1+0', 'month': 4, 'day': 15, 'hour': 16, 'name': 'Bullet'},
        {'time_control': '3+2', 'month': 8, 'day': 20, 'hour': 18, 'name': 'Blitz'},
        {'time_control': '5+0', 'month': 12, 'day': 31, 'hour': 14, 'name': 'Rapid'},
    ]
    
    for wc in world_cup_schedule:
        # Determine which year's World Cup to create
        wc_year = year
        world_cup_date = datetime(wc_year, wc['month'], wc['day'], wc['hour'], 0, 0)
        
        # If this World Cup has passed, schedule next year's
        if current_time > world_cup_date + timedelta(days=7):
            wc_year += 1
            world_cup_date = datetime(wc_year, wc['month'], wc['day'], wc['hour'], 0, 0)
        
        # Check if this specific World Cup already exists
        existing = any(
            t.get('tournament_type') == 'world_cup' and
            t.get('time_control') == wc['time_control'] and
            datetime.fromisoformat(t['start_time']).year == wc_year and
            datetime.fromisoformat(t['start_time']).month == wc['month']
            for t in tournaments.values()
        )
        
        if not existing:
            tournament_id = str(uuid.uuid4())
            config = TOURNAMENT_TYPES['world_cup']
            
            tournaments[tournament_id] = {
                'id': tournament_id,
                'name': f"Mill World Cup {wc_year} {wc['time_control']}",
                'tournament_type': 'world_cup',
                'time_control': wc['time_control'],
                'duration': config['duration'],
                'start_time': world_cup_date.isoformat(),
                'end_time': (world_cup_date + timedelta(minutes=config['duration'])).isoformat(),
                'status': 'scheduled',
                'players': {},
                'games': [],
                'color': config['color'],
                'leaderboard': [],
                'prizes': get_tournament_prizes('world_cup'),
                'is_world_cup': True  # Special flag to always show this
            }

def award_tournament_trophies(tournament_id, tournament):
    """Award trophies to players when tournament ends"""
    tournament_type = tournament.get('tournament_type', 'daily')
    tournament_name = tournament.get('name', 'Tournament')
    finished_date = datetime.now().strftime('%Y-%m-%d')
    
    # Get sorted leaderboard
    players = tournament.get('players', {})
    sorted_players = sorted(
        players.items(),
        key=lambda x: (x[1].get('score', 0), -x[1].get('games_played', 0)),
        reverse=True
    )
    
    if not sorted_players:
        return
    
    # Award trophies based on tournament type
    if tournament_type == 'marathon':
        # Marathon trophies with globe icons and ranking numbers
        for rank, (username, data) in enumerate(sorted_players, 1):
            if username not in users:
                continue
            
            trophy = None
            if rank == 1:
                trophy = {
                    'type': 'marathon_1st',
                    'icon': 'fa-globe-americas',
                    'icon_size': 'xlarge',
                    'icon_color': '#FFD700',
                    'name': f'{tournament_name} - 1st Place',
                    'date': finished_date,
                    'tournament_id': tournament_id,
                    'rank': rank,
                    'show_number': True
                }
            elif rank == 2:
                trophy = {
                    'type': 'marathon_2nd',
                    'icon': 'fa-globe-americas',
                    'icon_size': 'xlarge',
                    'icon_color': '#C0C0C0',
                    'name': f'{tournament_name} - 2nd Place',
                    'date': finished_date,
                    'tournament_id': tournament_id,
                    'rank': rank,
                    'show_number': True
                }
            elif rank == 3:
                trophy = {
                    'type': 'marathon_3rd',
                    'icon': 'fa-globe-americas',
                    'icon_size': 'xlarge',
                    'icon_color': '#CD7F32',
                    'name': f'{tournament_name} - 3rd Place',
                    'date': finished_date,
                    'tournament_id': tournament_id,
                    'rank': rank,
                    'show_number': True
                }
            elif rank <= 10:
                trophy = {
                    'type': 'marathon_top10',
                    'icon': 'fa-globe-americas',
                    'icon_size': 'large',
                    'icon_color': '#4CAF50',
                    'name': f'{tournament_name} - Top 10',
                    'date': finished_date,
                    'tournament_id': tournament_id,
                    'rank': rank
                }
            elif rank <= 100:
                trophy = {
                    'type': 'marathon_top100',
                    'icon': 'fa-globe-americas',
                    'icon_size': 'medium',
                    'icon_color': '#2196F3',
                    'name': f'{tournament_name} - Top 100',
                    'date': finished_date,
                    'tournament_id': tournament_id,
                    'rank': rank
                }
            elif rank <= 500:
                trophy = {
                    'type': 'marathon_top500',
                    'icon': 'fa-globe-americas',
                    'icon_size': 'small',
                    'icon_color': '#FFFFFF',
                    'name': f'{tournament_name} - Top 500',
                    'date': finished_date,
                    'tournament_id': tournament_id,
                    'rank': rank
                }
            
            if trophy:
                if 'trophies' not in users[username]:
                    users[username]['trophies'] = []
                users[username]['trophies'].append(trophy)
                save_user_to_db(username)
    
    elif tournament_type == 'world_cup':
        # World Cup: Only top 3 get crown trophies with numbers
        crown_colors = {
            1: '#FFD700',
            2: '#C0C0C0',
            3: '#CD7F32'
        }
        crown_names = {
            1: '1st Place',
            2: '2nd Place',
            3: '3rd Place'
        }
        
        for rank, (username, data) in enumerate(sorted_players[:3], 1):
            if username not in users:
                continue
            
            trophy = {
                'type': f'world_cup_{rank}',
                'icon': 'fa-crown',
                'icon_size': 'xlarge',
                'icon_color': crown_colors[rank],
                'name': f'{tournament_name} - {crown_names[rank]}',
                'date': finished_date,
                'tournament_id': tournament_id,
                'rank': rank,
                'show_number': True
            }
            
            if 'trophies' not in users[username]:
                users[username]['trophies'] = []
            users[username]['trophies'].append(trophy)
            save_user_to_db(username)
    
    archive_data = {
        'id': tournament_id,
        'name': tournament_name,
        'tournament_type': tournament_type,
        'time_control': tournament.get('time_control', '3+2'),
        'start_time': tournament.get('start_time'),
        'end_time': tournament.get('end_time'),
        'finished_time': tournament.get('finished_time'),
        'status': 'finished',
        'players': dict(players),
        'color': tournament.get('color', '#FFD700'),
        'final_leaderboard': [(username, data) for username, data in sorted_players]
    }
    archived_tournaments[tournament_id] = archive_data
    
    try:
        with app.app_context():
            existing = ArchivedTournament.query.get(tournament_id)
            if existing:
                existing.players = archive_data['players']
                existing.final_leaderboard = archive_data['final_leaderboard']
            else:
                archived_entry = ArchivedTournament(
                    id=tournament_id,
                    name=tournament_name,
                    tournament_type=tournament_type,
                    time_control=archive_data['time_control'],
                    start_time=archive_data['start_time'],
                    end_time=archive_data['end_time'],
                    finished_time=archive_data['finished_time'],
                    status='finished',
                    players=archive_data['players'],
                    color=archive_data['color'],
                    final_leaderboard=archive_data['final_leaderboard']
                )
                db.session.add(archived_entry)
            db.session.commit()
    except Exception as e:
        print(f"[TOURNAMENT] Error saving archived tournament to DB: {e}")
    
    print(f"[TOURNAMENT] Awarded trophies for {tournament_name} ({tournament_type}) and archived")

def start_scheduled_tournaments():
    """Check and start scheduled tournaments and cleanup finished ones"""
    current_time = datetime.now()
    finished_tournaments = []

    for tournament_id, tournament in tournaments.items():
        if tournament['status'] == 'scheduled':
            start_time = datetime.fromisoformat(tournament['start_time'])
            if current_time >= start_time:
                tournament['status'] = 'active'
                print(f"Tournament {tournament_id} is now active! Starting initial pairing...")
                # Start a background thread to run initial pairing
                import threading
                def initial_pairing(tid):
                    time.sleep(1)  # Small delay to let clients connect
                    run_tournament_pairing_round(tid)
                threading.Thread(target=initial_pairing, args=(tournament_id,), daemon=True).start()
        elif tournament['status'] == 'active':
            end_time = datetime.fromisoformat(tournament['end_time'])
            if current_time >= end_time:
                tournament['status'] = 'finished'
                # Mark when tournament finished for removal timing
                tournament['finished_time'] = current_time.isoformat()
                # Award trophies to players
                award_tournament_trophies(tournament_id, tournament)
        elif tournament['status'] == 'finished':
            # Check if tournament has been finished for 5 minutes
            finished_time = tournament.get('finished_time')
            if finished_time:
                finished_time_obj = datetime.fromisoformat(finished_time)
                if current_time >= finished_time_obj + timedelta(minutes=5):
                    finished_tournaments.append(tournament_id)
            else:
                # Fallback for tournaments that finished before this update
                end_time = datetime.fromisoformat(tournament['end_time'])
                if current_time >= end_time + timedelta(minutes=5):
                    finished_tournaments.append(tournament_id)

    # Remove finished tournaments
    for tournament_id in finished_tournaments:
        del tournaments[tournament_id]

def get_tournament_prizes(tournament_type):
    """Get prizes for tournament type"""
    current_year = datetime.now().year
    if tournament_type == 'marathon':
        return {
            1: {'trophy': 'marathon_winner', 'globe': 'biggest'},
            10: {'globe': 'big'},
            100: {'globe': 'medium'},
            500: {'globe': 'small'}
        }
    elif tournament_type == 'world_cup':
        return {
            1: {'trophy': 'world_cup_winner', 'year': current_year, 'title': f'Mill World Cup Winner {current_year}'}
        }
    return {}

# Socket.IO events for real-time gameplay
@socketio.on('connect')
def on_connect(auth=None):
    print(f"Socket connected: {request.sid}")

    if 'username' in session:
        username = session['username']
        if username not in banned_users:
            online_users[request.sid] = username
            unique_users = len(set(online_users.values()))
            print(f"User {username} connected. Online count: {unique_users}")

            # Broadcast updated count to all clients
            socketio.emit('user_count', unique_users)
            
            # Broadcast online status change for this user
            socketio.emit('user_online_status', {'username': username, 'online': True})

            # Send welcome message to connected user
            emit('connect_success', {'message': f'Welcome {username}!'})
            
            # Send initial pause status
            emit('pause_status', {'paused': username in paused_users, 'username': username})
            
            # Check for pending rank notification (if user was promoted/demoted while offline)
            if username in users and users[username].get('pending_rank_notification'):
                pending = users[username]['pending_rank_notification']
                print(f"[RANK] Sending pending rank notification to {username}: {pending}")
                emit('rank_changed', pending)
                # Clear the pending notification
                users[username]['pending_rank_notification'] = None
                save_user_to_db(username)
            
            # Check for pending admin tournament invites
            if username in admin_tournament_invites:
                pending_invites = admin_tournament_invites[username]
                for tournament_id in pending_invites[:]:  # Copy list to allow modification
                    if tournament_id in tournaments:
                        tournament = tournaments[tournament_id]
                        # Only send if tournament is still active/scheduled
                        if tournament.get('status') in ['active', 'scheduled']:
                            print(f"[INVITE] Sending pending tournament invite to {username}: {tournament['name']}")
                            emit('admin_tournament_invite', {
                                'tournament_id': tournament_id,
                                'tournament_name': tournament['name'],
                                'invited_by': tournament.get('created_by', 'Admin')
                            })
                    else:
                        # Tournament no longer exists, remove from pending
                        pending_invites.remove(tournament_id)

            # Check for active game and redirect immediately (Lichess-style)
            active_game_id = get_active_game_for_player(username)
            if active_game_id:
                game_data = games.get(active_game_id)
                if game_data:
                    opponent = game_data['black'] if username == game_data['white'] else game_data['white']
                    emit('active_game_found', {
                        'room_id': active_game_id,
                        'opponent': opponent,
                        'time_control': game_data.get('time_control', '3+2'),
                        'your_color': 'white' if username == game_data['white'] else 'black'
                    })
        else:
            emit('user_banned', {
                'message': 'You have been banned from the experience',
                'reason': 'You are permanently banned from MillELO',
                'show_logout_only': True
            })
            return False
    else:
        # Guest user
        emit('user_count', len(online_users))

@socketio.on('disconnect')
def on_disconnect(reason=None):
    print(f"Socket disconnected: {request.sid}")

    if request.sid in online_users:
        username = online_users[request.sid]
        del online_users[request.sid]
        unique_users = len(set(online_users.values()))
        print(f"User {username} disconnected. Online count: {unique_users}")
        
        # Check if user still has other active connections (after SID removal above)
        user_still_connected = username in set(online_users.values())
        
        # Broadcast online status change if user is completely offline
        if not user_still_connected:
            socketio.emit('user_online_status', {'username': username, 'online': False})

        if not user_still_connected:
            # User is completely disconnected - give them a shorter grace period to reconnect quickly
            def handle_disconnection():
                # Wait 5 seconds before checking reconnection (enough time to quickly refresh/navigate back)
                time.sleep(5)

                # Check again if user has reconnected
                user_reconnected = any(user == username for sid, user in online_users.items())

                if not user_reconnected:
                    # Check if user has any active games
                    active_game_id = get_active_game_for_player(username)
                    if active_game_id:
                        game_data = games.get(active_game_id)
                        if game_data and game_data.get('status') == 'playing':
                            # User is still disconnected after grace period - but don't end the game
                            # Let their timer run out naturally instead of giving them an automatic loss
                            print(f"Player {username} disconnected but game will continue until timer expires naturally")
                        else:
                            print(f"Player {username} disconnected, but game {active_game_id} is already finished")
                    else:
                        print(f"Player {username} disconnected with no active game")

            # Start disconnection handler in a separate thread
            threading.Thread(target=handle_disconnection, daemon=True).start()

        # Clean up any seeking rooms for this user
        rooms_to_remove = []
        for room_id, room in list(game_rooms.items()):
            if room.get('seeking', False) and username in room.get('players', []):
                rooms_to_remove.append(room_id)

        for room_id in rooms_to_remove:
            if room_id in game_rooms:
                del game_rooms[room_id]
                print(f"Cleaned up seeking room {room_id} for disconnected user {username}")

        # Broadcast updated count
        socketio.emit('user_count', unique_users)

@socketio.on('admin_command')
def handle_admin_command(data):
    print(f"[ADMIN_CMD] Received admin command: {data}")
    username = session.get('username')
    if not username:
        print(f"[ADMIN_CMD] No username in session")
        return
    
    user_data = users.get(username, {})
    user_rank = user_data.get('admin_rank')
    print(f"[ADMIN_CMD] User: {username}, Rank: {user_rank}")
    
    # Check if user has any admin rank
    if not user_rank or user_rank not in ADMIN_RANKS:
        print(f"[ADMIN_CMD] User {username} not authorized")
        return

    command = data.get('command', '').strip()
    parts = command.split()
    print(f"[ADMIN_CMD] Command: '{command}', Parts: {parts}")

    if not parts:
        return

    cmd = parts[0].lower()
    print(f"[ADMIN_CMD] Executing cmd: {cmd}")
    global admin_invisible_mode, admin_god_mode

    if cmd == 'close':
        emit('admin_close')

    elif cmd == 'boardsetup':
        # Open the board setup modal for piece design customization
        emit('open_board_setup')

    elif cmd == 'banlist':
        if user_rank not in ['admin', 'dragon', 'galaxy', 'creator']:
            emit('admin_response', {'error': 'Banlist requires Admin rank or higher'})
            return
        emit('open_banlist')

    elif cmd == 'promote' and len(parts) >= 3:
        # promote <username> <rank>
        target_user = parts[1]
        target_rank = parts[2].lower()
        
        if target_user not in users:
            emit('admin_response', {'error': f'User {target_user} not found'})
            return
        
        if target_rank not in ADMIN_RANKS:
            emit('admin_response', {'error': f'Invalid rank. Valid ranks: {", ".join(ADMIN_RANKS[:-1])}'})  # Don't show creator
            return
        
        if target_rank == 'creator':
            emit('admin_response', {'error': 'Cannot promote to creator rank'})
            return
        
        # Check if promoter can promote to this rank
        if not can_promote_to(user_rank, target_rank):
            emit('admin_response', {'error': f'You cannot promote users to {target_rank} rank'})
            return
        
        # Set the target user's rank
        users[target_user]['admin_rank'] = target_rank
        users[target_user]['is_admin'] = True
        save_user_to_db(target_user)
        emit('admin_response', {'message': f'Promoted {target_user} to {target_rank.upper()} rank'})
        
        # Notify the promoted user in real-time
        target_sid = None
        for sid, uname in online_users.items():
            if uname == target_user:
                target_sid = sid
                break
        rank_notification = {
            'type': 'promoted',
            'new_rank': target_rank,
            'by': username,
            'message': f'You have been promoted by {username} to {target_rank.upper()}'
        }
        if target_sid:
            print(f"[RANK] Emitting rank_changed PROMOTE to SID {target_sid} for user {target_user}")
            socketio.emit('rank_changed', rank_notification, room=target_sid)
            print(f"[RANK] rank_changed PROMOTE event emitted successfully")
        else:
            # User is offline - store pending notification
            print(f"[RANK] User {target_user} is offline, storing pending rank notification")
            users[target_user]['pending_rank_notification'] = rank_notification
            save_user_to_db(target_user)

    elif cmd == 'demote' and len(parts) >= 2:
        # demote <username>
        target_user = parts[1]
        
        if target_user not in users:
            emit('admin_response', {'error': f'User {target_user} not found'})
            return
        
        target_rank = users[target_user].get('admin_rank')
        
        if not target_rank:
            emit('admin_response', {'error': f'{target_user} is not an admin'})
            return
        
        # Check if demoter can demote this rank
        if not can_demote(user_rank, target_rank):
            emit('admin_response', {'error': f'You cannot demote {target_rank.upper()} rank users'})
            return
        
        # Demote to the next lower rank or remove completely
        current_rank_level = get_admin_rank_level(target_rank)
        if current_rank_level > 1:
            # Demote to lower rank
            new_rank_index = ADMIN_RANKS.index(target_rank) - 1
            new_rank = ADMIN_RANKS[new_rank_index] if new_rank_index >= 0 else None
        else:
            new_rank = None
        
        users[target_user]['admin_rank'] = new_rank
        users[target_user]['is_admin'] = new_rank is not None
        save_user_to_db(target_user)
        
        if new_rank:
            emit('admin_response', {'message': f'Demoted {target_user} from {target_rank.upper()} to {new_rank.upper()}'})
        else:
            emit('admin_response', {'message': f'Demoted {target_user} (was {target_rank.upper()}, now no admin)'})
        
        # Notify the demoted user in real-time
        target_sid = None
        for sid, uname in online_users.items():
            if uname == target_user:
                target_sid = sid
                break
        rank_notification = {
            'type': 'demoted',
            'new_rank': new_rank,
            'old_rank': target_rank,
            'by': username,
            'message': f'You have been demoted by {username}' + (f' to {new_rank.upper()}' if new_rank else ' (no longer admin)')
        }
        if target_sid:
            print(f"[RANK] Emitting rank_changed DEMOTE to SID {target_sid} for user {target_user}")
            socketio.emit('rank_changed', rank_notification, room=target_sid)
            print(f"[RANK] rank_changed DEMOTE event emitted successfully")
        else:
            # User is offline - store pending notification
            print(f"[RANK] User {target_user} is offline, storing pending demote notification")
            users[target_user]['pending_rank_notification'] = rank_notification
            save_user_to_db(target_user)

    elif cmd == 'setelo':
        # Creator only command
        if user_rank != 'creator':
            emit('admin_response', {'error': 'This command requires Creator rank'})
            return
        # Handle format: setelo Frut blitz:2000 or setelo Frut blitz 2000
        if len(parts) >= 3:
            target_user = parts[1]
            if ':' in parts[2]:  # Format: blitz:2000
                rating_part = parts[2].split(':')
                rating_type = rating_part[0]
                try:
                    new_rating = int(rating_part[1])
                except (ValueError, IndexError):
                    emit('admin_response', {'error': 'Invalid format. Use: setelo username rating_type:value or setelo username rating_type value'})
                    return
            elif len(parts) >= 4:  # Format: blitz 2000
                rating_type = parts[2]
                try:
                    new_rating = int(parts[3])
                except ValueError:
                    emit('admin_response', {'error': 'Invalid rating value'})
                    return
            else:
                emit('admin_response', {'error': 'Usage: setelo username rating_type:value or setelo username rating_type value'})
                return

            if target_user in users and rating_type in ['bullet', 'blitz']:
                users[target_user][f'{rating_type}_rating'] = max(600, new_rating)
                save_user_to_db(target_user)
                emit('admin_response', {'message': f'Set {target_user} {rating_type} rating to {new_rating}'})
            else:
                emit('admin_response', {'error': f'User {target_user} not found or invalid rating type (use bullet/blitz)'})
        else:
            emit('admin_response', {'error': 'Usage: setelo username rating_type:value or setelo username rating_type value'})

    elif cmd == 'setcolourname' and len(parts) >= 3:
        target_user = parts[1]
        color = parts[2]
        if target_user not in users:
            emit('admin_response', {'error': f'User {target_user} not found'})
            return
        
        # Check rank hierarchy - can't affect higher or equal ranks
        target_user_rank = users[target_user].get('admin_rank')
        if target_user_rank and get_admin_rank_level(target_user_rank) >= get_admin_rank_level(user_rank):
            emit('admin_response', {'error': f'Cannot modify {target_user_rank.upper()} rank users'})
            return
        
        users[target_user]['color'] = color
        save_user_to_db(target_user)
        emit('admin_response', {'message': f'Changed {target_user} name color to {color}'})

    elif cmd == 'like' and len(parts) >= 2:
        target_user = parts[1]
        if target_user not in users:
            emit('admin_response', {'error': f'User {target_user} not found'})
            return
        if target_user == username:
            emit('admin_response', {'error': 'Cannot like your own profile'})
            return
        # Initialize likes if not present
        if 'likes' not in users[target_user]:
            users[target_user]['likes'] = {'count': 0, 'liked_by': []}
        # Check if already liked
        if username in users[target_user]['likes'].get('liked_by', []):
            emit('admin_response', {'error': f'You have already liked {target_user}\'s profile'})
            return
        # Add like
        users[target_user]['likes']['count'] = users[target_user]['likes'].get('count', 0) + 1
        users[target_user]['likes']['liked_by'].append(username)
        save_user_to_db(target_user)
        emit('admin_response', {'message': f'Liked {target_user}\'s profile! They now have {users[target_user]["likes"]["count"]} likes'})

    elif cmd == 'spawntournament':
        # Create a daily arena tournament with admin's name
        # Usage: spawntournament [duration] - duration like 1, 2.30, etc (max 3 hours)
        print(f"[ADMIN] spawntournament command triggered by {username}")
        
        # Parse duration argument (default 1 hour)
        duration_hours = 1.0
        duration_minutes = 0
        
        if len(parts) >= 2:
            try:
                duration_str = parts[1]
                if '.' in duration_str:
                    # Format: hours.minutes (e.g., 2.30 = 2 hours 30 minutes)
                    h_part, m_part = duration_str.split('.')
                    duration_hours = int(h_part) if h_part else 0
                    duration_minutes = int(m_part) if m_part else 0
                else:
                    # Just hours (e.g., 2 = 2 hours)
                    duration_hours = float(duration_str)
                    duration_minutes = 0
            except ValueError:
                emit('admin_response', {'error': 'Invalid duration format. Use: spawntournament 1 (1 hour) or spawntournament 2.30 (2 hours 30 minutes)'})
                return
        
        # Calculate total minutes and cap at 3 hours (180 minutes)
        total_minutes = int(duration_hours * 60) + duration_minutes
        if total_minutes <= 0:
            emit('admin_response', {'error': 'Duration must be greater than 0'})
            return
        if total_minutes > 180:
            total_minutes = 180
            emit('admin_response', {'message': 'Duration capped at maximum 3 hours'})
        
        # Use naive datetime (same as other tournaments)
        now = datetime.now()
        
        tournament_id = str(uuid.uuid4())
        tournament_name = f"{username} Arena"
        
        # Tournament starts now
        start_time = now
        end_time = now + timedelta(minutes=total_minutes)
        
        # Format duration for display
        display_hours = total_minutes // 60
        display_mins = total_minutes % 60
        if display_hours > 0 and display_mins > 0:
            duration_display = f"{display_hours}h {display_mins}m"
        elif display_hours > 0:
            duration_display = f"{display_hours}h"
        else:
            duration_display = f"{display_mins}m"
        
        tournament = {
            'id': tournament_id,
            'name': tournament_name,
            'tournament_type': 'daily',
            'time_control': '1+0',
            'duration': total_minutes,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'status': 'active',
            'players': {},
            'games': [],
            'color': '#4CAF50',
            'leaderboard': [],
            'prizes': {},
            'created_by': username
        }
        tournaments[tournament_id] = tournament
        print(f"[ADMIN] Created tournament: {tournament_name} (ID: {tournament_id[:8]}...) by {username}")
        
        # Broadcast tournament creation to all clients
        socketio.emit('tournament_created', {
            'id': tournament_id,
            'name': tournament_name,
            'tournament_type': 'daily',
            'time_control': '1+0',
            'duration': total_minutes,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'status': 'active',
            'color': '#4CAF50'
        })
        
        # Also emit tournaments_updated to refresh lobby/tournaments page in real-time
        socketio.emit('tournaments_updated', {'action': 'refresh'})
        
        emit('admin_response', {'message': f'Created tournament: {tournament_name} ({duration_display}) - ID: {tournament_id[:8]}...'})

    elif cmd == 'createadmintournament':
        global admin_tournament_counter
        # Create an admin-only tournament
        print(f"[ADMIN] createadmintournament command triggered by {username}")
        
        admin_tournament_counter += 1
        tournament_name = f"admin{admin_tournament_counter}"
        
        # Tournament starts now, lasts 1 hour
        now = datetime.now()
        tournament_id = str(uuid.uuid4())
        start_time = now
        end_time = now + timedelta(hours=1)
        
        tournament = {
            'id': tournament_id,
            'name': tournament_name,
            'tournament_type': 'admin',
            'time_control': '1+0',
            'duration': 60,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'status': 'active',
            'players': {},
            'games': [],
            'color': '#000000',
            'leaderboard': [],
            'prizes': {},
            'created_by': username,
            'admin_only': True,
            'invited_users': []
        }
        tournaments[tournament_id] = tournament
        print(f"[ADMIN] Created admin tournament: {tournament_name} (ID: {tournament_id[:8]}...) by {username}")
        
        # Broadcast tournament creation
        socketio.emit('tournament_created', {
            'id': tournament_id,
            'name': tournament_name,
            'tournament_type': 'admin',
            'time_control': '1+0',
            'duration': 60,
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'status': 'active',
            'color': '#000000',
            'admin_only': True
        })
        socketio.emit('tournaments_updated', {'action': 'refresh'})
        
        emit('admin_response', {'message': f'Created admin tournament: {tournament_name} - ID: {tournament_id[:8]}...'})

    elif cmd == 'invite' and len(parts) >= 4 and parts[2].lower() == 'to':
        # Format: invite <username> to <tournament_name>
        target_user = parts[1]
        tournament_name = parts[3].lower()
        
        print(f"[ADMIN] invite command: {username} inviting {target_user} to {tournament_name}")
        
        # Find the admin tournament by name
        target_tournament = None
        for tid, t in tournaments.items():
            if t.get('admin_only') and t['name'].lower() == tournament_name:
                target_tournament = t
                break
        
        if not target_tournament:
            emit('admin_response', {'error': f'Admin tournament "{tournament_name}" not found'})
            return
        
        if target_user not in users:
            emit('admin_response', {'error': f'User {target_user} not found'})
            return
        
        # Add user to invited list
        if target_user not in target_tournament.get('invited_users', []):
            if 'invited_users' not in target_tournament:
                target_tournament['invited_users'] = []
            target_tournament['invited_users'].append(target_user)
        
        # Track invite for user
        if target_user not in admin_tournament_invites:
            admin_tournament_invites[target_user] = []
        if target_tournament['id'] not in admin_tournament_invites[target_user]:
            admin_tournament_invites[target_user].append(target_tournament['id'])
        
        # Send notification to user if online
        target_sid = None
        for sid, uname in online_users.items():
            if uname == target_user:
                target_sid = sid
                break
        
        if target_sid:
            socketio.emit('admin_tournament_invite', {
                'tournament_id': target_tournament['id'],
                'tournament_name': target_tournament['name'],
                'invited_by': username
            }, room=target_sid)
        
        emit('admin_response', {'message': f'Invited {target_user} to {tournament_name}'})

    elif cmd == 'removetitle' and len(parts) >= 2:
        # Creator only command
        if user_rank != 'creator':
            emit('admin_response', {'error': 'This command requires Creator rank'})
            return
        target_user = parts[1]
        if target_user in users:
            users[target_user]['highest_title'] = None
            users[target_user]['highest_title_color'] = None
            users[target_user]['bullet_title'] = None
            users[target_user]['bullet_title_color'] = None
            users[target_user]['blitz_title'] = None
            users[target_user]['blitz_title_color'] = None
            save_user_to_db(target_user)
            emit('admin_response', {'message': f'Removed all titles from {target_user}'})
        else:
            emit('admin_response', {'error': f'User {target_user} not found'})

    elif cmd == 'ban' and len(parts) >= 2:
        target_user = parts[1]
        ban_reason = ' '.join(parts[2:]) if len(parts) > 2 else 'No reason given'
        # Cannot ban creator or users with higher/equal rank
        target_user_rank = users.get(target_user, {}).get('admin_rank')
        if target_user_rank == 'creator':
            emit('admin_response', {'error': 'Cannot ban the creator'})
            return
        # Cannot ban users with same or higher rank
        if target_user_rank and get_admin_rank_level(target_user_rank) >= get_admin_rank_level(user_rank):
            emit('admin_response', {'error': f'Cannot ban {target_user_rank.upper()} rank users'})
            return
        if target_user in users:
            banned_users.add(target_user)
            
            try:
                ban_record = BanRecord(
                    banned_user=target_user,
                    banned_by=username,
                    reason=ban_reason
                )
                db.session.add(ban_record)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Failed to save ban record: {e}")
            
            # Find ALL sessions for the banned user and disconnect them with ban message
            banned_user_sids = []
            for sid, user_data in list(online_users.items()):
                if user_data == target_user:
                    banned_user_sids.append(sid)
            
            # Send ban message to all sessions of the banned user
            for sid in banned_user_sids:
                socketio.emit('user_banned', {
                    'message': 'You have been permanently banned from MillELO',
                    'reason': 'Your account has been permanently banned by an administrator',
                    'show_logout_only': True
                }, to=sid)
                
            # Remove from online users
            for sid in banned_user_sids:
                if sid in online_users:
                    del online_users[sid]
            
            emit('admin_response', {'message': f'Permanently banned {target_user}. They cannot log in again.'})
        elif target_user == 'Frut':
            emit('admin_response', {'error': 'Cannot ban admin user'})
        else:
            emit('admin_response', {'error': f'User {target_user} not found'})

    elif cmd == 'unban' and len(parts) >= 2:
        target_user = parts[1]
        banned_users.discard(target_user)
        try:
            active_bans = BanRecord.query.filter_by(banned_user=target_user, is_active=True).all()
            for ban in active_bans:
                ban.is_active = False
                ban.unbanned_by = username
                ban.unbanned_at = datetime.now().isoformat()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Failed to update ban records: {e}")
        emit('admin_response', {'message': f'Unbanned {target_user}'})


    elif cmd == 'reset' and len(parts) >= 2:
        # Creator only command
        if user_rank != 'creator':
            emit('admin_response', {'error': 'This command requires Creator rank'})
            return
        target_user = parts[1]
        # Cannot reset creator
        if users.get(target_user, {}).get('admin_rank') == 'creator':
            emit('admin_response', {'error': 'Cannot reset creator account'})
            return
        if target_user in users:
            users[target_user].update({
                'bullet_rating': 100,
                'blitz_rating': 100,
                'games_played': {'bullet': 0, 'blitz': 0},
                'wins': {'bullet': 0, 'blitz': 0},
                'losses': {'bullet': 0, 'blitz': 0},
                'draws': {'bullet': 0, 'blitz': 0},
                'best_wins': {'bullet': [], 'blitz': []},
                'tournaments_won': {'daily': 0, 'weekly': 0, 'monthly': 0, 'marathon': 0, 'world_cup': 0},
                'trophies': [],
                'elo_history': {'bullet': [], 'blitz': []}
            })
            emit('admin_response', {'message': f'Reset {target_user} statistics'})

    elif cmd == 'announce' and len(parts) >= 2:
        # Dragon+ command
        if user_rank not in ['dragon', 'galaxy', 'creator']:
            emit('admin_response', {'error': 'This command requires Dragon rank or higher'})
            return
        message = ' '.join(parts[1:])
        user_info = users.get(username, {})
        color_name = user_info.get('color_name', '#ffffff')
        piece_design = user_info.get('piece_design', 'circle')
        socketio.emit('admin_announcement', {
            'message': message,
            'username': username,
            'color_name': color_name,
            'piece_design': piece_design,
            'admin_rank': user_rank
        })
        emit('admin_response', {'message': f'Announcement sent: {message}'})

    elif cmd == 'createtournament':
        print(f"[ADMIN] createtournament command triggered by {username} (rank: {user_rank})")
        # Creator only command
        if user_rank != 'creator':
            print(f"[ADMIN] createtournament denied - user {username} is not creator")
            emit('admin_response', {'error': 'This command requires Creator rank'})
            return
        # Format: createtournament <type> <time_control>
        # Examples: createtournament weekly 1+0, createtournament monthly 3+2
        print(f"[ADMIN] createtournament parts: {parts}")
        if len(parts) >= 3:
            tournament_type = parts[1].lower()
            time_control = parts[2]
            
            # Validate tournament type
            valid_types = ['daily', 'weekly', 'monthly', 'marathon', 'worldcup', 'world_cup']
            if tournament_type not in valid_types:
                emit('admin_response', {'error': f'Invalid type. Use: {", ".join(valid_types[:-1])}'})
                return
            
            # Normalize world_cup
            if tournament_type == 'worldcup':
                tournament_type = 'world_cup'
            
            # Validate time control
            valid_time_controls = ['1+0', '3+2', '5+0']
            if time_control not in valid_time_controls:
                emit('admin_response', {'error': f'Invalid time control. Use: {", ".join(valid_time_controls)}'})
                return
            
            config = TOURNAMENT_TYPES.get(tournament_type, TOURNAMENT_TYPES['daily'])
            tournament_id = str(uuid.uuid4())
            current_time = datetime.now()
            
            # Create tournament name
            if tournament_type == 'world_cup':
                name = f'Mill World Cup {current_time.year} {time_control}'
            else:
                name = f'{config["name"]} {time_control}'
            
            tournaments[tournament_id] = {
                'id': tournament_id,
                'name': name,
                'tournament_type': tournament_type,
                'time_control': time_control,
                'duration': config['duration'],
                'start_time': current_time.isoformat(),
                'end_time': (current_time + timedelta(minutes=config['duration'])).isoformat(),
                'status': 'active',
                'players': {},
                'games': [],
                'color': config['color'],
                'leaderboard': [],
                'prizes': get_tournament_prizes(tournament_type)
            }
            emit('admin_response', {'message': f'Created {name} (ID: {tournament_id[:8]}...)'})
            # Broadcast to refresh tournament lists
            socketio.emit('tournaments_updated', {})
        else:
            emit('admin_response', {'error': 'Usage: createtournament <type> <time_control> (e.g. createtournament weekly 1+0)'})

    elif cmd == 'listtournaments':
        # Creator only command
        if user_rank != 'creator':
            emit('admin_response', {'error': 'This command requires Creator rank'})
            return
        active_tournaments = []
        for tid, t in tournaments.items():
            if t.get('status') == 'active':
                active_tournaments.append({
                    'id': tid[:8],
                    'full_id': tid,
                    'name': t.get('name', 'Unknown'),
                    'players': len(t.get('players', {})),
                    'time_control': t.get('time_control', '?')
                })
        if active_tournaments:
            msg = 'Active tournaments:\n' + '\n'.join([
                f"  {t['id']} - {t['name']} ({t['players']} players, {t['time_control']})"
                for t in active_tournaments
            ])
            emit('admin_response', {'message': msg})
        else:
            emit('admin_response', {'message': 'No active tournaments'})

    elif cmd == 'endtournament' and len(parts) >= 2:
        # Creator only command
        if user_rank != 'creator':
            emit('admin_response', {'error': 'This command requires Creator rank'})
            return
        search_id = parts[1].lower()
        found_tournament = None
        for tid, t in tournaments.items():
            if tid.lower().startswith(search_id) and t.get('status') == 'active':
                found_tournament = (tid, t)
                break
        
        if found_tournament:
            tid, tournament = found_tournament
            tournament['status'] = 'finished'
            tournament['end_time'] = datetime.now().isoformat()
            tournament['finished_time'] = datetime.now().isoformat()
            
            # Award trophies and archive tournament
            award_tournament_trophies(tid, tournament)
            
            # Mark all active tournament games as not counting for points
            for game_id, game in games.items():
                if game.get('tournament_id') == tid and game.get('status') != 'finished':
                    game['tournament_points_disabled'] = True
            
            # Get top 3 players for podium display
            sorted_players = sorted(
                tournament.get('players', {}).items(),
                key=lambda x: (x[1].get('score', 0), x[1].get('wins', 0)),
                reverse=True
            )[:3]
            
            top_players = []
            for username, data in sorted_players:
                user_data = users.get(username, {})
                wins = data.get('wins', 0)
                games_played = data.get('games_played', 1)
                win_rate = round((wins / games_played) * 100) if games_played > 0 else 0
                # Get rating based on tournament time control
                time_control = tournament.get('time_control', '3+2')
                base_time = int(time_control.split('+')[0]) if '+' in time_control else 3
                rating_type = 'bullet_rating' if base_time <= 2 else 'blitz_rating'
                player_rating = user_data.get(rating_type, 1500)
                # Get ranking color for player
                ranking_color = get_ranking_color(username)
                
                top_players.append({
                    'username': username,
                    'score': data.get('score', 0),
                    'games_played': games_played,
                    'wins': wins,
                    'win_rate': win_rate,
                    'color': ranking_color or user_data.get('color', '#c9c9c9'),
                    'rating': player_rating,
                    'admin_rank': user_data.get('admin_rank')
                })
            
            # Notify all players in the tournament
            socketio.emit('tournament_ended', {
                'tournament_id': tid,
                'name': tournament.get('name', 'Tournament'),
                'message': 'Tournament Finished!',
                'top_players': top_players
            })
            
            emit('admin_response', {'message': f'Ended tournament: {tournament.get("name")} ({tid[:8]})'})
        else:
            emit('admin_response', {'error': f'No active tournament found with ID starting with "{search_id}". Use listtournaments to see active ones.'})

    else:
        emit('admin_response', {'error': 'Unknown command. Type "cmd" for help.'})

# Available piece designs
PIECE_DESIGNS = {
    'classic': {'name': 'Classic Circle', 'shape': 'circle', 'icon': '●'},
    'star': {'name': 'Star', 'shape': 'star', 'icon': '★'},
    'diamond': {'name': 'Diamond', 'shape': 'diamond', 'icon': '◆'},
    'heart': {'name': 'Heart', 'shape': 'heart', 'icon': '♥'},
    'crown': {'name': 'Crown', 'shape': 'crown', 'icon': '♕'},
    'moon': {'name': 'Moon', 'shape': 'moon', 'icon': '☽'},
    'sun': {'name': 'Sun', 'shape': 'sun', 'icon': '☀'},
    'flower': {'name': 'Flower', 'shape': 'flower', 'icon': '✿'},
    'bolt': {'name': 'Lightning', 'shape': 'bolt', 'icon': '⚡'},
    'shield': {'name': 'Shield', 'shape': 'shield', 'icon': '🛡'},
    'gem': {'name': 'Gem', 'shape': 'gem', 'icon': '💎'},
    'flame': {'name': 'Flame', 'shape': 'flame', 'icon': '🔥'}
}

@socketio.on('set_piece_design')
def on_set_piece_design(data):
    username = session.get('username')
    if not username or username not in users:
        return
    
    design = data.get('design', 'classic')
    if design not in PIECE_DESIGNS:
        design = 'classic'
    
    users[username]['piece_design'] = design
    
    # Save to database
    try:
        user = User.query.filter_by(username=username).first()
        if user:
            user.piece_design = design
            db.session.commit()
    except Exception as e:
        print(f"Error saving piece design: {e}")
    
    emit('piece_design_updated', {'design': design, 'info': PIECE_DESIGNS[design]})

@socketio.on('get_piece_designs')
def on_get_piece_designs():
    username = session.get('username')
    current_design = 'classic'
    if username and username in users:
        current_design = users[username].get('piece_design', 'classic')
    emit('piece_designs_list', {'designs': PIECE_DESIGNS, 'current': current_design})

@socketio.on('pause_tournament')
def on_pause_tournament(data):
    username = session.get('username')
    if not username:
        return

    tournament_id = data.get('tournament_id')

    if username in paused_users:
        paused_users.remove(username)
        auto_paused_users.discard(username)
        emit('pause_status', {'paused': False, 'message': 'You are no longer paused', 'tournament_id': tournament_id, 'username': username})
        socketio.emit('user_pause_status', {'username': username, 'paused': False}, room=None)
        if tournament_id and tournament_id in tournaments:
            match_tournament_players(tournament_id, username)
    else:
        paused_users.add(username)
        emit('pause_status', {'paused': True, 'message': 'You are now paused from tournaments', 'tournament_id': tournament_id, 'username': username})
        # Broadcast to all users that this user is paused
        socketio.emit('user_pause_status', {'username': username, 'paused': True}, room=None)

@socketio.on('join_tournament_page')
def on_join_tournament_page(data):
    """Track when a user is on the tournament page"""
    username = session.get('username')
    if not username:
        return
    
    tournament_id = data.get('tournament_id')
    if not tournament_id:
        return
    
    # User is now on the tournament page
    tournament_page_users[username] = tournament_id
    
    # Remove from pending auto-pause since they're back
    if username in pending_auto_pause:
        pending_auto_pause.discard(username)
    
    # If they were auto-paused, unpause them (but not if they manually paused)
    if username in paused_users and username in auto_paused_users:
        paused_users.remove(username)
        auto_paused_users.discard(username)
        emit('pause_status', {'paused': False, 'message': 'Welcome back! You are active in the tournament.', 'tournament_id': tournament_id, 'username': username})
        socketio.emit('user_pause_status', {'username': username, 'paused': False}, room=None)
        # Try to pair them
        if tournament_id in tournaments:
            match_tournament_players(tournament_id, username)
    
    print(f"User {username} joined tournament page {tournament_id[:8]}...")

@socketio.on('leave_tournament_page')
def on_leave_tournament_page(data):
    """Track when a user leaves the tournament page - auto-pause if needed"""
    username = session.get('username')
    if not username:
        return
    
    tournament_id = data.get('tournament_id')
    
    # Remove from tournament page tracking
    if username in tournament_page_users:
        del tournament_page_users[username]
    
    # Check if user is in an active tournament game
    if tournament_id and tournament_id in tournaments:
        tournament = tournaments[tournament_id]
        # Only auto-pause if tournament is active and user is a participant
        if tournament.get('status') == 'active' and username in tournament.get('players', {}):
            # Check if user is currently in a game
            active_game = get_active_game_for_player(username)
            if active_game:
                # Mark for auto-pause after game ends
                pending_auto_pause.add(username)
                print(f"User {username} left tournament page while in game - will auto-pause after game")
            else:
                # Auto-pause immediately since they're not in a game
                if username not in paused_users:
                    paused_users.add(username)
                    auto_paused_users.add(username)
                    socketio.emit('pause_status', {'paused': True, 'message': 'You have been paused (left tournament page)', 'tournament_id': tournament_id, 'username': username})
                    socketio.emit('user_pause_status', {'username': username, 'paused': True}, room=None)
                    print(f"User {username} auto-paused for leaving tournament page")

@socketio.on('request_tournament_pairing')
def on_request_tournament_pairing(data):
    """Player requests to be paired in a tournament"""
    username = session.get('username')
    if not username:
        return
    
    tournament_id = data.get('tournament_id')
    if not tournament_id or tournament_id not in tournaments:
        return
    
    tournament = tournaments[tournament_id]
    if tournament.get('status') != 'active':
        return
    
    # Check if player is in this tournament
    if username not in tournament.get('players', {}):
        return
    
    # Clear player from game menu so they can be paired
    players_in_game_menu.discard(username)
    game_menu_timestamps.pop(username, None)
    
    # Check if player is paused or in a game
    if username in paused_users:
        emit('pairing_status', {'status': 'paused', 'message': 'You are paused. Click Continue to start playing.'})
        return
    
    if is_player_in_game(username):
        # Find their current game and send them there
        active_game = get_active_game_for_player(username)
        if active_game:
            emit('tournament_game_start', {'room_id': active_game})
        return
    
    # Try to pair this player
    print(f"Player {username} requesting pairing in tournament {tournament_id}")
    match_tournament_players(tournament_id, username)
    
    # If still not paired, let them know we're searching
    if not is_player_in_game(username):
        emit('pairing_status', {'status': 'searching', 'message': 'Looking for opponent...'})

def get_active_game_for_player(username):
    """Find if player has an active game they can rejoin"""
    for game_id, game_data in games.items():
        if (username in [game_data.get('white'), game_data.get('black')] and 
            game_data.get('status') in ['playing', 'waiting_first_move'] and
            game_data.get('status') != 'canceled'):
            return game_id
    return None

@socketio.on('check_active_game')
def on_check_active_game(data=None):
    """Check if player has an active game to rejoin"""
    username = session.get('username')
    if not username:
        print(f"Check active game called but no username in session")
        return

    # Check if this is being called from watch mode
    is_watching = data.get('watching', False) if data else False

    active_game_id = get_active_game_for_player(username)
    print(f"Checking active game for {username}: {'Found ' + active_game_id if active_game_id else 'None found'} (watching: {is_watching})")

    if active_game_id and not is_watching:
        # Get game details for better notification
        game_data = games.get(active_game_id)
        if game_data:
            opponent = game_data['black'] if username == game_data['white'] else game_data['white']
            print(f"Sending active_game_found to {username} for game {active_game_id} vs {opponent}")

            # Only redirect to active game when not watching
            emit('active_game_found', {
                'room_id': active_game_id,
                'opponent': opponent,
                'time_control': game_data.get('time_control', '3+2'),
                'your_color': 'white' if username == game_data['white'] else 'black'
            })
        else:
            print(f"Active game {active_game_id} found for {username} but no game data")
    else:
        # Always send a response, even if no active game or if watching
        emit('no_active_game', {'message': 'No active game found' if not active_game_id else 'Watching mode - no redirect'})

@socketio.on('join_lobby')
def on_join_lobby():
    """Join the lobby room to receive featured game updates"""
    from flask import request as flask_request
    
    join_room('lobby')
    sid = flask_request.sid
    print(f"Client joined lobby room, SID: {sid}")
    # Send current featured game immediately - use emit() which sends to calling client
    featured = get_featured_game()
    if featured:
        print(f"Sending featured game to lobby client {sid}: {featured.get('game_id')}, board: {featured.get('board')[:3]}...")
        # Use standard emit() for direct response to calling client
        emit('featured_game_update', featured)
        print(f"Emit completed for featured_game_update to {sid}")
    else:
        print(f"No featured game available for new lobby client")
        emit('featured_game_update', {'game_id': None})

@socketio.on('get_featured_game')
def on_get_featured_game():
    """Get the current featured game for lobby display"""
    from flask import request as flask_request
    
    featured = get_featured_game()
    sid = flask_request.sid
    print(f"Featured game request from SID {sid}: {featured.get('game_id') if featured else 'None'}, Total games: {len(games)}, Games with timers: {len(game_timers)}")
    if featured:
        emit('featured_game_update', featured)
        print(f"Emit completed for get_featured_game to {sid}")
    else:
        emit('featured_game_update', {'game_id': None})

@socketio.on('join_tournament_room')
def on_join_tournament_room(data):
    """Join the tournament room to receive featured game updates"""
    tournament_id = data.get('tournament_id')
    if not tournament_id:
        return
    
    room_name = f"tournament_{tournament_id}"
    join_room(room_name)
    print(f"Client joined tournament room: {room_name}")

@socketio.on('get_tournament_featured_game')
def on_get_tournament_featured_game(data):
    """Get the featured game for a tournament (top player's current game)"""
    tournament_id = data.get('tournament_id')
    if not tournament_id or tournament_id not in tournaments:
        emit('tournament_featured_game_update', {'tournament_id': tournament_id, 'game_id': None})
        return
    
    featured = get_tournament_featured_game(tournament_id)
    if featured:
        featured['tournament_id'] = tournament_id
        emit('tournament_featured_game_update', featured)
    else:
        emit('tournament_featured_game_update', {'tournament_id': tournament_id, 'game_id': None})

def get_tournament_featured_game(tournament_id):
    """Get the featured game for a tournament - the #1 player's current game"""
    if tournament_id not in tournaments:
        return None
    
    tournament = tournaments[tournament_id]
    players_data = tournament.get('players', {})
    
    if not players_data:
        return None
    
    # Sort players by score (descending), then by ELO for tie-breaking
    def sort_key(item):
        username, data = item
        score = data.get('score', 0)
        if username in users:
            rating_type = 'bullet' if tournament.get('time_control', '3+2').split('+')[0].isdigit() and int(tournament.get('time_control', '3+2').split('+')[0]) <= 2 else 'blitz'
            rating = users[username].get(f'{rating_type}_rating', 1500)
        else:
            rating = 1500
        return (-score, -rating)
    
    sorted_players = sorted(players_data.items(), key=sort_key)
    
    # Find the first top player who is currently in a game from THIS tournament
    for username, _ in sorted_players:
        active_game_id = get_active_game_for_player(username)
        if active_game_id and active_game_id in games:
            game_data = games[active_game_id]
            
            # CRITICAL: Only show games from this specific tournament
            if game_data.get('tournament_id') != tournament_id:
                continue
            
            # Only show games that are actually playing or waiting for first move
            if game_data.get('status') not in ['playing', 'waiting_first_move']:
                continue
            
            white = game_data.get('white')
            black = game_data.get('black')
            
            white_data = users.get(white, {})
            black_data = users.get(black, {})
            
            rating_type = 'bullet' if game_data.get('time_control', '3+2').split('+')[0].isdigit() and int(game_data.get('time_control', '3+2').split('+')[0]) <= 2 else 'blitz'
            
            white_rating = white_data.get(f'{rating_type}_rating', 1500)
            black_rating = black_data.get(f'{rating_type}_rating', 1500)
            
            white_title, white_title_color = get_title(white_rating, white_data)
            black_title, black_title_color = get_title(black_rating, black_data)
            
            # Get timer info
            timer_obj = game_timers.get(active_game_id)
            if timer_obj and hasattr(timer_obj, 'white_time'):
                white_time = timer_obj.white_time
                black_time = timer_obj.black_time
            else:
                white_time = 180
                black_time = 180
            
            # Get the actual board from the game object
            game_obj = game_data.get('game')
            actual_board = game_obj.board if game_obj else [None] * 24
            current_player = game_obj.current_player if game_obj else 'white'
            
            # Use ranking color if available, otherwise use user color
            white_ranking_color = get_ranking_color(white)
            black_ranking_color = get_ranking_color(black)
            
            return {
                'game_id': active_game_id,
                'white': white,
                'black': black,
                'white_rating': white_rating,
                'black_rating': black_rating,
                'white_title': white_title,
                'white_title_color': white_title_color,
                'black_title': black_title,
                'black_title_color': black_title_color,
                'white_color': white_ranking_color or white_data.get('color', '#c9c9c9'),
                'black_color': black_ranking_color or black_data.get('color', '#888888'),
                'white_admin_rank': white_data.get('admin_rank'),
                'black_admin_rank': black_data.get('admin_rank'),
                'time_control': game_data.get('time_control', '3+2'),
                'board': actual_board,
                'current_player': current_player,
                'white_time': white_time,
                'black_time': black_time,
                'tournament_id': tournament_id
            }
    
    return None

def broadcast_tournament_featured_game(tournament_id):
    """Broadcast the featured game update to all clients viewing this tournament"""
    featured = get_tournament_featured_game(tournament_id)
    room_name = f"tournament_{tournament_id}"
    
    if featured:
        featured['tournament_id'] = tournament_id
        socketio.emit('tournament_featured_game_update', featured, room=room_name)
    else:
        socketio.emit('tournament_featured_game_update', {'tournament_id': tournament_id, 'game_id': None}, room=room_name)

@socketio.on('seek_game')
def on_seek_game(data):
    username = session.get('username')
    if not username:
        emit('error', {'message': 'Not logged in'})
        return

    if username in banned_users:
        emit('error', {'message': 'You are banned and cannot play games'})
        return

    if username in paused_users:
        emit('error', {'message': 'You are paused and cannot play games'})
        return

    # Check if player already has an active game
    active_game_id = get_active_game_for_player(username)
    if active_game_id:
        emit('error', {'message': 'You have an active game! Redirecting...'})
        emit('active_game_found', {'room_id': active_game_id})
        return

    time_control = data.get('time_control', '3+2')
    print(f"User {username} seeking game with time control {time_control}")

    # Remove user from any existing seeking rooms first
    rooms_to_remove = []
    for room_id, room in list(game_rooms.items()):
        if username in room.get('players', []):
            rooms_to_remove.append(room_id)

    for room_id in rooms_to_remove:
        if room_id in game_rooms:
            del game_rooms[room_id]
            print(f"Removed {username} from existing room {room_id}")

    # Try to match with another player seeking the same time control
    matched = False
    for room_id, room in list(game_rooms.items()):
        if (room.get('seeking', False) and 
            len(room['players']) == 1 and 
            room['time_control'] == time_control and 
            room['players'][0] != username):

            # Match found!
            opponent = room['players'][0]
            print(f"Matched {username} with {opponent}")

            # Randomly assign colors
            players = [username, opponent]
            random.shuffle(players)
            white_player = players[0]
            black_player = players[1]

            # Parse time control
            parts = time_control.split('+')
            minutes = int(parts[0])
            increment = int(parts[1]) if len(parts) > 1 else 0
            base_time = minutes * 60

            # Create game
            game = NineMensMorris()
            game_id = str(uuid.uuid4())
            games[game_id] = {
                'id': game_id,
                'white': white_player,
                'black': black_player,
                'game': game,
                'moves': [],
                'positions': [game.board[:]],
                'time_control': time_control,
                'start_time': datetime.now().isoformat(),
                'game_started_at': time.time(),
                'status': 'waiting_first_move',
                'berserk': {'white': False, 'black': False},
                'timers': {'white': base_time, 'black': base_time},
                'increment': increment,
                'last_move_time': datetime.now(),
                'server_start_time': time.time(),
                'active_timer': 'white',
                'first_move_deadline': time.time() + 20,
                'white_first_move_made': False,
                'black_first_move_made': False,
                'waiting_for_first_move': 'white'
            }

            # Start server-authoritative timer (handles first move countdown)
            start_game_timer(game_id)

            # Remove seeking room
            del game_rooms[room_id]

            # Get both players' session IDs
            current_player_sid = request.sid
            opponent_sids = []

            # Find ALL session IDs for the opponent (they might have multiple connections)
            for sid, user_data in online_users.items():
                if user_data == opponent:
                    opponent_sids.append(sid)

            # Add current player to the game room
            join_room(game_id, sid=current_player_sid)

            # Add all opponent sessions to the game room
            for opponent_sid in opponent_sids:
                join_room(game_id, sid=opponent_sid)

            # Prepare game data for both players
            game_data = {
                'room_id': game_id,
                'white': white_player,
                'black': black_player,
                'time_control': time_control
            }

            # Get user data for both players
            white_user_data = users.get(white_player, {})
            black_user_data = users.get(black_player, {})
            
            # Add ranking colors to user data
            white_user_data['ranking_color'] = get_ranking_color(white_player)
            black_user_data['ranking_color'] = get_ranking_color(black_player)

            # Calculate initial piece counts
            piece_counts = calculate_piece_counts(games[game_id])

            # Get ranking badges for both players
            white_badges = get_ranking_badge(white_player)
            black_badges = get_ranking_badge(black_player)
            
            # Send individual notifications with player colors and user data to current player
            socketio.emit('players_matched', {
                **game_data,
                'your_color': 'white' if username == white_player else 'black',
                'white_user_data': white_user_data,
                'black_user_data': black_user_data,
                'timers': {'white': base_time, 'black': base_time},
                'piece_counts': piece_counts,
                'white_badges': white_badges,
                'black_badges': black_badges
            }, to=current_player_sid)

            # Send individual notifications with player colors and user data to ALL opponent sessions
            for opponent_sid in opponent_sids:
                socketio.emit('players_matched', {
                    **game_data,
                    'your_color': 'white' if opponent == white_player else 'black',
                    'white_user_data': white_user_data,
                    'black_user_data': black_user_data,
                    'timers': {'white': base_time, 'black': base_time},
                    'piece_counts': piece_counts,
                    'white_badges': white_badges,
                    'black_badges': black_badges
                }, to=opponent_sid)

            print(f"Match created: {white_player} vs {black_player} in room {game_id}")
            
            # Send first move countdown start signal to all players in the game room
            socketio.emit('first_move_countdown_start', {
                'seconds_left': 20,
                'server_start_time': time.time(),
                'waiting_for': 'white'
            }, room=game_id)

            matched = True
            break

    if not matched:
        # No match found, create new seeking room
        room_id = str(uuid.uuid4())
        game_rooms[room_id] = {
            'players': [username],
            'time_control': time_control,
            'seeking': True,
            'created_at': datetime.now().isoformat()
        }
        join_room(room_id)
        emit('waiting_for_opponent', {'time_control': time_control})
        print(f"Created seeking room for {username} with time control {time_control}")

@socketio.on('cancel_seek')
def on_cancel_seek():
    username = session.get('username')
    if not username:
        return

    # Remove user from any seeking rooms
    rooms_to_remove = []
    for room_id, room in game_rooms.items():
        if username in room.get('players', []) and room.get('seeking', False):
            rooms_to_remove.append(room_id)

    for room_id in rooms_to_remove:
        leave_room(room_id)
        del game_rooms[room_id]

    emit('seek_cancelled')

@socketio.on('make_move')
def on_make_move(data):
    username = session.get('username')
    room_id = data['room_id']
    game_data = games.get(room_id)

    if not game_data or username not in [game_data['white'], game_data['black']]:
        return

    # Check if game was canceled or finished
    if game_data.get('status') in ['canceled', 'finished']:
        emit('game_canceled', {
            'reason': 'Game is no longer active',
            'message': 'This game has been canceled or finished',
            'redirect_to_lobby': True
        })
        return

    game = game_data['game']
    player_color = 'white' if username == game_data['white'] else 'black'

    # Check if it's this player's turn (considering waiting_for_removal state)
    if game.current_player != player_color and not game_data.get('waiting_for_removal'):
        return

    # Handle first move timer for tournament games - BOTH players get 20 seconds each
    if game_data.get('status') == 'waiting_first_move':
        if player_color == 'white' and not game_data.get('white_first_move_made', False):
            # White made their first move - now start 20 second timer for black
            game_data['white_first_move_made'] = True
            game_data['waiting_for_first_move'] = 'black'
            game_data['first_move_deadline'] = time.time() + 20
            print(f"White made first move - black now has 20 seconds")
            
            # Emit countdown restart for black's first move
            socketio.emit('first_move_countdown_start', {
                'seconds_left': 20,
                'server_start_time': time.time(),
                'waiting_for': 'black'
            }, room=room_id)
        elif player_color == 'black' and not game_data.get('black_first_move_made', False):
            # Black made their first move - game can start normally
            game_data['black_first_move_made'] = True
            game_data['waiting_for_first_move'] = None
            game_data['status'] = 'playing'
            game_data['timer_started'] = True
            # Resume the timer for normal play
            with timer_lock:
                if room_id in game_timers:
                    game_timers[room_id].resume()
                    game_timers[room_id].state = TIMER_RUNNING
            print(f"Black made first move - game starting normally")
            
            # Emit signal that first move phase is complete
            socketio.emit('first_move_countdown_complete', {}, room=room_id)
    elif not game_data.get('timer_started', False):
        # For non-tournament games, start timer on first move
        game_data['timer_started'] = True
        # Resume the timer for the first move
        with timer_lock:
            if room_id in game_timers:
                game_timers[room_id].resume()
                game_timers[room_id].state = TIMER_RUNNING
            game_data['status'] = 'playing'

    # Special handling for piece removal after mill

    # Special handling for piece removal after mill
    if game_data.get('waiting_for_removal') and data.get('remove_pos') is not None:
        # Player is removing a piece after mill formation
        remove_pos = data.get('remove_pos')

        # Validate the removal is legal
        if game.can_remove(remove_pos):
            # Remove the piece from the board
            game.board[remove_pos] = None

            # Clear waiting state and switch player
            game_data['waiting_for_removal'] = False
            game.current_player = 'black' if game.current_player == 'white' else 'white'

            # Switch timer to new player
            switch_player_timer(room_id, game.current_player)

            # Add move to history
            game.moves.append({
                'from': None,
                'to': None,
                'remove': remove_pos,
                'player': player_color,
                'mill': False
            })

            # Calculate updated piece counts
            piece_counts = calculate_piece_counts(game_data)

            # Always emit the updated board state to both players
            socketio.emit('move_made', {
                'board': game.board,
                'current_player': game.current_player,
                'phase': game.phase,
                'move': game.moves[-1],
                'waiting_for_removal': False,
                'piece_counts': piece_counts
            }, room=room_id)
            
            # Broadcast tournament featured game update after piece removal
            tournament_id = game_data.get('tournament_id')
            if tournament_id:
                broadcast_tournament_featured_game(tournament_id)

            # Check for game over after piece removal
            winner = game.get_winner()
            if winner:
                # Immediately set game as finished
                game_data['status'] = 'finished'
                game_data['winner'] = winner
                game_data['end_reason'] = 'normal'
                game_data['end_time'] = datetime.now().isoformat()

                # Stop the timer immediately
                stop_game_timer(room_id)

                # Update ratings and get the changes
                update_ratings(game_data, winner)

                # Get updated user data for both players
                white_user = users[game_data['white']]
                black_user = users[game_data['black']]
                rating_type = get_rating_type(game_data.get('time_control', '3+2'))

                # Send game over with complete rating information
                socketio.emit('game_over', {
                    'winner': winner,
                    'reason': 'normal',
                    'rating_changes': game_data.get('rating_changes', {}),
                    'new_ratings': {
                        game_data['white']: white_user[f'{rating_type}_rating'],
                        game_data['black']: black_user[f'{rating_type}_rating']
                    },
                    'rating_type': rating_type
                }, room=room_id)
                update_tournament_scores(game_data, winner)
                requeue_tournament_players(game_data)

                print(f"Game {room_id} finished after piece removal - {winner} wins, timer stopped")

        return

    # Normal move handling
    result = game.make_move(
        data.get('from_pos'),
        data['to_pos'],
        data.get('remove_pos')
    )

    if result and result['success']:
        game_data['moves'] = game.moves
        game_data['positions'].append(game.board[:])

        # Add move timestamp and timer info to moves
        with timer_lock:
            timer = game_timers.get(room_id)
            if timer:
                current_times = timer.get_current_times()
                game_data['moves'][-1]['timestamp'] = datetime.now().isoformat()
                game_data['moves'][-1]['timers'] = {
                    'white': current_times['white'],
                    'black': current_times['black']
                }

        # Handle waiting_for_removal state
        if result.get('mill_formed') and result.get('waiting_for_removal'):
            # Mill formed but no piece removed yet and pieces can be removed - set waiting state
            game_data['waiting_for_removal'] = True
            # Keep same player active for piece removal
            game.current_player = player_color
        else:
            # No mill formed, piece was removed, or no pieces can be removed - clear any waiting state
            if 'waiting_for_removal' in game_data:
                del game_data['waiting_for_removal']
            # Switch timer to new player
            switch_player_timer(room_id, game.current_player)

        # Calculate updated piece counts
        piece_counts = calculate_piece_counts(game_data)

        # Always emit the updated board state to both players
        socketio.emit('move_made', {
            'board': game.board,
            'current_player': game.current_player,
            'phase': game.phase,
            'move': game.moves[-1],
            'waiting_for_removal': game_data.get('waiting_for_removal', False),
            'piece_counts': piece_counts
        }, room=room_id)
        
        # Broadcast featured game update to lobby room
        featured = get_featured_game()
        if featured and featured.get('game_id') == room_id:
            print(f"Broadcasting featured game update to lobby room for game {room_id}")
            socketio.emit('featured_game_update', featured, room='lobby', namespace='/')
        
        # Broadcast tournament featured game update if this is a tournament game
        tournament_id = game_data.get('tournament_id')
        if tournament_id:
            broadcast_tournament_featured_game(tournament_id)

        winner = game.get_winner()
        if winner:
            # Immediately set game as finished to prevent further moves
            game_data['status'] = 'finished'
            game_data['winner'] = winner
            game_data['end_reason'] = 'normal'
            game_data['end_time'] = datetime.now().isoformat()

            # Stop the timer immediately
            stop_game_timer(room_id)

            # Update ratings and get the changes
            update_ratings(game_data, winner)

            # Get updated user data for both players
            white_user = users[game_data['white']]
            black_user = users[game_data['black']]
            rating_type = get_rating_type(game_data.get('time_control', '3+2'))

            # Send game over with complete rating information
            socketio.emit('game_over', {
                'winner': winner,
                'reason': 'normal',
                'rating_changes': game_data.get('rating_changes', {}),
                'new_ratings': {
                    game_data['white']: white_user[f'{rating_type}_rating'],
                    game_data['black']: black_user[f'{rating_type}_rating']
                },
                'rating_type': rating_type
            }, room=room_id)
            update_tournament_scores(game_data, winner)
            requeue_tournament_players(game_data)

            print(f"Game {room_id} finished normally - {winner} wins, timer stopped")

def calculate_k_factor(rating, games_played):
    """Calculate K-factor based on Lichess system without high volatility for new players"""
    if rating < 1500:
        return 32  # Lower rated players
    elif rating < 2000:
        return 24  # Intermediate players
    else:
        return 16  # Strong players

def update_ratings(game_data, winner):
    """Update player ratings after game with standard ELO system"""
    white_player = users[game_data['white']]
    black_player = users[game_data['black']]

    rating_type = get_rating_type(game_data.get('time_control', '3+2'))
    game_type = game_data.get('game_type', 'ranked')

    # Get current ratings
    white_rating = white_player[f'{rating_type}_rating']
    black_rating = black_player[f'{rating_type}_rating']

    # If it's a friendly game, don't update ratings
    if game_type == 'friendly':
        # Store zero rating changes for display
        game_data['rating_changes'] = {
            game_data['white']: 0,
            game_data['black']: 0
        }
        
        # Still update game statistics but not ratings
        white_player['games_played'][rating_type] += 1
        black_player['games_played'][rating_type] += 1

        if winner == 'white':
            white_player['wins'][rating_type] += 1
            black_player['losses'][rating_type] += 1
        elif winner == 'black':
            black_player['wins'][rating_type] += 1
            white_player['losses'][rating_type] += 1
        else:
            white_player['draws'][rating_type] += 1
            black_player['draws'][rating_type] += 1

        return

    # Get games played for K-factor calculation
    white_games = white_player['games_played'][rating_type]
    black_games = black_player['games_played'][rating_type]

    # Calculate K-factors based on rating only
    white_k = calculate_k_factor(white_rating, white_games)
    black_k = calculate_k_factor(black_rating, black_games)

    # Standard ELO calculation
    expected_white = 1 / (1 + 10**((black_rating - white_rating) / 400))
    expected_black = 1 - expected_white

    # Determine actual scores
    if winner == 'white':
        white_score, black_score = 1.0, 0.0
    elif winner == 'black':
        white_score, black_score = 0.0, 1.0
    else:  # draw
        white_score, black_score = 0.5, 0.5

    # Calculate rating changes
    white_rating_change = white_k * (white_score - expected_white)
    black_rating_change = black_k * (black_score - expected_black)

    # Round rating changes
    white_rating_change = round(white_rating_change)
    black_rating_change = round(black_rating_change)

    # Ensure minimum rating change for decisive games (except draws)
    if winner != 'draw':
        if abs(white_rating_change) < 1:
            white_rating_change = 1 if white_score > 0.5 else -1
        if abs(black_rating_change) < 1:
            black_rating_change = 1 if black_score > 0.5 else -1
    
    # 400 ELO gap protection: if winner has 400+ more ELO, they get 0 (anti-farming)
    elo_gap = abs(white_rating - black_rating)
    if elo_gap >= 400 and winner != 'draw':
        if winner == 'white' and white_rating > black_rating:
            # White won and had 400+ more ELO - no ELO gain for white
            white_rating_change = 0
        elif winner == 'black' and black_rating > white_rating:
            # Black won and had 400+ more ELO - no ELO gain for black
            black_rating_change = 0

    # Apply rating floor
    white_new_rating = max(50, white_rating + white_rating_change)
    black_new_rating = max(50, black_rating + black_rating_change)

    # Update ratings
    white_player[f'{rating_type}_rating'] = white_new_rating
    black_player[f'{rating_type}_rating'] = black_new_rating

    # Recalculate and update highest titles after rating change
    white_max_rating = max(white_player.get('bullet_rating', 100), white_player.get('blitz_rating', 100))
    black_max_rating = max(black_player.get('bullet_rating', 100), black_player.get('blitz_rating', 100))
    get_title(white_max_rating, white_player)  # This will update highest_title if needed
    get_title(black_max_rating, black_player)  # This will update highest_title if needed

    # Store rating changes for display
    game_data['rating_changes'] = {
        game_data['white']: white_rating_change,
        game_data['black']: black_rating_change
    }

    # Track ELO history
    current_time = datetime.now().isoformat()

    # Ensure elo_history exists for both players
    if 'elo_history' not in white_player:
        white_player['elo_history'] = {'bullet': [], 'blitz': []}
    if 'elo_history' not in black_player:
        black_player['elo_history'] = {'bullet': [], 'blitz': []}

    white_player['elo_history'][rating_type].append({
        'rating': white_new_rating,
        'change': white_rating_change,
        'date': current_time,
        'opponent': black_player['username'],
        'result': 'win' if winner == 'white' else ('loss' if winner == 'black' else 'draw')
    })
    black_player['elo_history'][rating_type].append({
        'rating': black_new_rating,
        'change': black_rating_change,
        'date': current_time,
        'opponent': white_player['username'],
        'result': 'win' if winner == 'black' else ('loss' if winner == 'white' else 'draw')
    })

    # Update game statistics
    white_player['games_played'][rating_type] += 1
    black_player['games_played'][rating_type] += 1

    if winner == 'white':
        white_player['wins'][rating_type] += 1
        black_player['losses'][rating_type] += 1
        update_best_wins(white_player, black_player, rating_type, game_data.get('id'))
    elif winner == 'black':
        black_player['wins'][rating_type] += 1
        white_player['losses'][rating_type] += 1
        update_best_wins(black_player, white_player, rating_type, game_data.get('id'))
    else:
        white_player['draws'][rating_type] += 1
        black_player['draws'][rating_type] += 1

    print(f"Rating calculation details:")
    print(f"  Winner: {winner}")
    print(f"  White: {white_rating} -> {white_new_rating} ({white_rating_change:+d})")
    print(f"  Black: {black_rating} -> {black_new_rating} ({black_rating_change:+d})")
    print(f"  Expected scores: White {expected_white:.3f}, Black {expected_black:.3f}")
    print(f"  K-factors: White {white_k}, Black {black_k}")
    
    # Emit real-time rating update to all clients
    socketio.emit('rating_update', {
        'players': {
            game_data['white']: {
                'new_rating': white_new_rating,
                'change': white_rating_change,
                'rating_type': rating_type
            },
            game_data['black']: {
                'new_rating': black_new_rating,
                'change': black_rating_change,
                'rating_type': rating_type
            }
        },
        'tournament_id': game_data.get('tournament_id')
    })
    
    # Save updated ratings to database
    save_user_to_db(game_data['white'])
    save_user_to_db(game_data['black'])
    
    # Save game to database
    game_id = game_data.get('id')
    if game_id:
        save_game_to_db(game_id)

def update_best_wins(winner, loser, rating_type, game_id=None):
    """Update best wins list"""
    loser_rating = loser[f'{rating_type}_rating']
    best_wins = winner['best_wins'][rating_type]

    best_wins.append({
        'opponent': loser['username'],
        'opponent_rating': loser_rating,
        'date': datetime.now().isoformat(),
        'game_id': game_id
    })

    # Keep only top 5 best wins by rating (can include multiple wins against same opponent)
    best_wins.sort(key=lambda x: x['opponent_rating'], reverse=True)
    winner['best_wins'][rating_type] = best_wins[:5]

def get_last_opponent(tournament_id, username):
    """Get the last opponent for a player in a tournament (to prevent immediate rematches)"""
    if tournament_id not in tournament_recent_opponents:
        return None
    return tournament_recent_opponents[tournament_id].get(username)

def set_last_opponent(tournament_id, player1, player2):
    """Track that two players just played against each other - stores only the last opponent"""
    if tournament_id not in tournament_recent_opponents:
        tournament_recent_opponents[tournament_id] = {}
    
    # Set player2 as last opponent of player1
    tournament_recent_opponents[tournament_id][player1] = player2
    
    # Set player1 as last opponent of player2
    tournament_recent_opponents[tournament_id][player2] = player1

def lichess_style_pairing(tournament_id):
    """
    Lichess Arena-style pairing algorithm:
    1. Sort all available players by tournament score (descending)
    2. Pair adjacent players (1vs2, 3vs4, etc.)
    3. NEVER pair players who just played against each other (must play someone else first)
    4. Skip paused players, players in games, and players in after-game menu
    """
    tournament = tournaments.get(tournament_id)
    if not tournament or tournament['status'] != 'active':
        return []
    
    # Get all available players (not paused, not in game, not in game menu)
    available_players = []
    for player_name, player_data in tournament['players'].items():
        if (player_name not in paused_users and 
            player_name not in players_in_game_menu and 
            not is_player_in_game(player_name)):
            available_players.append({
                'username': player_name,
                'score': player_data.get('score', 0),
                'rating': player_data.get('rating', 100)
            })
    
    # Sort by score (descending), then by rating (descending) as tiebreaker
    available_players.sort(key=lambda x: (x['score'], x['rating']), reverse=True)
    
    # Create pairings using Swiss-style (1vs2, 3vs4, etc.)
    pairings = []
    paired = set()
    
    for i, player in enumerate(available_players):
        if player['username'] in paired:
            continue
        
        # Get the last opponent for this player (they can't be paired with them again)
        last_opponent = get_last_opponent(tournament_id, player['username'])
        
        best_opponent = None
        for j in range(i + 1, len(available_players)):
            candidate = available_players[j]
            if candidate['username'] in paired:
                continue
            
            # NEVER pair with the last opponent - they must play someone else first
            if candidate['username'] == last_opponent:
                continue
            
            # Also check if this player was the candidate's last opponent
            candidate_last_opponent = get_last_opponent(tournament_id, candidate['username'])
            if player['username'] == candidate_last_opponent:
                continue
            
            best_opponent = candidate
            break
        
        # NO FALLBACK - if no valid opponent found, player must wait
        if best_opponent:
            pairings.append((player['username'], best_opponent['username']))
            paired.add(player['username'])
            paired.add(best_opponent['username'])
    
    return pairings

def parse_time_control(time_control):
    """Parse time control string like '3+2' into base_time (seconds) and increment"""
    parts = time_control.split('+')
    minutes = int(parts[0])
    increment = int(parts[1]) if len(parts) > 1 else 0
    base_time = minutes * 60  # Convert to seconds
    return base_time, increment

def get_tournament_rank(tournament_id, username):
    """Get a player's current rank in the tournament (1-indexed)"""
    tournament = tournaments.get(tournament_id)
    if not tournament or username not in tournament.get('players', {}):
        return None
    
    # Sort players by score (descending), then by rating (descending) as tiebreaker
    def get_sort_key(item):
        uname, data = item
        score = data.get('score', 0)
        rating = data.get('rating', 0)
        return (score, rating)
    
    sorted_players = sorted(tournament['players'].items(), key=get_sort_key, reverse=True)
    
    # Find the player's rank
    for rank, (uname, _) in enumerate(sorted_players, 1):
        if uname == username:
            return rank
    return None

def create_tournament_game(tournament_id, player1, player2):
    """Create a tournament game between two players"""
    tournament = tournaments.get(tournament_id)
    if not tournament:
        return None
    
    room_id = str(uuid.uuid4())
    game = NineMensMorris()
    
    # Randomly assign colors
    players = [player1, player2]
    random.shuffle(players)
    white_player = players[0]
    black_player = players[1]
    
    # Get tournament ranks for both players
    white_rank = get_tournament_rank(tournament_id, white_player)
    black_rank = get_tournament_rank(tournament_id, black_player)
    
    # Parse time control
    time_control = tournament.get('time_control', '3+2')
    base_time, increment = parse_time_control(time_control)
    
    games[room_id] = {
        'id': room_id,
        'white': white_player,
        'black': black_player,
        'white_rank': white_rank,
        'black_rank': black_rank,
        'game': game,
        'moves': [],
        'positions': [game.board[:]],
        'tournament_id': tournament_id,
        'time_control': time_control,
        'start_time': datetime.now().isoformat(),
        'game_started_at': time.time(),
        'berserk': {'white': False, 'black': False},
        'first_move_deadline': time.time() + 20,
        'white_first_move_made': False,
        'black_first_move_made': False,
        'waiting_for_first_move': 'white',
        'status': 'waiting_first_move',
        'timers': {
            'white': base_time,
            'black': base_time
        },
        'increment': increment
    }
    
    # Start the server-side timer for this game
    start_game_timer(room_id)
    
    # Track last opponents (prevents immediate rematches)
    set_last_opponent(tournament_id, white_player, black_player)
    
    # Find socket IDs for both players and emit to them
    white_sids = []
    black_sids = []
    for sid, username in online_users.items():
        if username == white_player:
            white_sids.append(sid)
        elif username == black_player:
            black_sids.append(sid)
    
    # Emit to white player's socket IDs
    for sid in white_sids:
        socketio.emit('tournament_game_start', {
            'room_id': room_id,
            'white': white_player,
            'black': black_player,
            'white_rank': white_rank,
            'black_rank': black_rank,
            'your_color': 'white',
            'tournament_id': tournament_id,
            'time_control': time_control
        }, room=sid)
    
    # Emit to black player's socket IDs
    for sid in black_sids:
        socketio.emit('tournament_game_start', {
            'room_id': room_id,
            'white': white_player,
            'black': black_player,
            'white_rank': white_rank,
            'black_rank': black_rank,
            'your_color': 'black',
            'tournament_id': tournament_id,
            'time_control': time_control
        }, room=sid)
    
    # Start first move timer - current player must move within 20 seconds
    def check_first_move_loop():
        while room_id in games:
            game_data = games.get(room_id)
            if not game_data:
                break
            
            # Check if game is still waiting for a first move
            if game_data.get('status') != 'waiting_first_move':
                break
            
            waiting_for = game_data.get('waiting_for_first_move')
            deadline = game_data.get('first_move_deadline', 0)
            
            if time.time() >= deadline:
                # Player failed to make first move - forfeit
                handle_first_move_timeout(room_id, waiting_for)
                break
            
            time.sleep(0.5)  # Check every 0.5 seconds
    
    import threading
    timer_thread = threading.Thread(target=check_first_move_loop, daemon=True)
    timer_thread.start()
    
    print(f"Tournament game created: {white_player} vs {black_player} in tournament {tournament_id}")
    
    # Broadcast updated tournament featured game
    broadcast_tournament_featured_game(tournament_id)
    
    return room_id

def handle_first_move_timeout(room_id, timed_out_player='white'):
    """Handle when a player fails to make first move within 20 seconds"""
    if room_id not in games:
        return
    
    game_data = games[room_id]
    if game_data.get('status') == 'finished':
        return
    
    white_player = game_data['white']
    black_player = game_data['black']
    tournament_id = game_data.get('tournament_id')
    
    # Determine winner based on who timed out
    if timed_out_player == 'white':
        winner = 'black'
        loser_name = white_player
        message = f'{white_player} failed to make first move in time'
    else:
        winner = 'white'
        loser_name = black_player
        message = f'{black_player} failed to make first move in time'
    
    # Mark game as finished
    game_data['status'] = 'finished'
    game_data['winner'] = winner
    game_data['result'] = f'{winner}_wins'
    game_data['result_reason'] = message
    
    # Stop any game timer
    stop_game_timer(room_id)
    
    # Update ELO ratings - first move timeout counts as a loss
    update_ratings(game_data, winner)
    
    # Get rating changes and new ratings for display
    rating_type = get_rating_type(game_data.get('time_control', '3+2'))
    rating_changes = game_data.get('rating_changes', {})
    new_ratings = {
        white_player: users[white_player][f'{rating_type}_rating'],
        black_player: users[black_player][f'{rating_type}_rating']
    }
    
    # Update tournament scores
    if tournament_id:
        update_tournament_scores(game_data, winner)
    
    # Notify both players with rating changes
    socketio.emit('game_over', {
        'winner': winner,
        'reason': 'first_move_timeout',
        'message': message,
        'rating_changes': rating_changes,
        'new_ratings': new_ratings,
        'rating_type': rating_type
    }, room=room_id)
    
    print(f"First move timeout: {loser_name} lost for not moving in time")
    
    # Re-queue BOTH players for next game if tournament still active
    requeue_tournament_players(game_data)

def requeue_tournament_players(game_data):
    """
    Add players to game menu when tournament game ends.
    Players must click 'Back to Tournament' or 'Analyse' buttons to continue.
    They cannot be paired while viewing the after-game menu.
    Also handles auto-pause for players who left tournament page during game.
    """
    tournament_id = game_data.get('tournament_id')
    if not tournament_id:
        return
    
    tournament = tournaments.get(tournament_id)
    if not tournament or tournament.get('status') != 'active':
        return
    
    white_player = game_data.get('white')
    black_player = game_data.get('black')
    
    # Check for pending auto-pause (player left tournament page during game)
    for player in [white_player, black_player]:
        if player and player in pending_auto_pause:
            pending_auto_pause.discard(player)
            if player not in paused_users:
                paused_users.add(player)
                auto_paused_users.add(player)
                socketio.emit('pause_status', {'paused': True, 'message': 'You have been paused (left tournament page)', 'tournament_id': tournament_id, 'username': player})
                socketio.emit('user_pause_status', {'username': player, 'paused': True}, room=None)
                print(f"User {player} auto-paused after game ended (was pending)")
    
    # Add both players to game menu (prevents pairing until they click a button)
    if white_player and white_player in tournament.get('players', {}):
        players_in_game_menu.add(white_player)
        game_menu_timestamps[white_player] = time.time()
        print(f"{white_player} added to game menu - waiting for button click")
    
    if black_player and black_player in tournament.get('players', {}):
        players_in_game_menu.add(black_player)
        game_menu_timestamps[black_player] = time.time()
        print(f"{black_player} added to game menu - waiting for button click")
    
    # Broadcast updated tournament featured game (may need to show different game now)
    broadcast_tournament_featured_game(tournament_id)

def run_tournament_pairing_round(tournament_id):
    """Run a full pairing round for the tournament"""
    tournament = tournaments.get(tournament_id)
    if not tournament or tournament['status'] != 'active':
        return
    
    # Auto-cleanup: Remove players stuck in game menu for more than 15 seconds
    # This prevents players from being permanently blocked from pairing
    current_time = time.time()
    stuck_players = [
        username for username, timestamp in game_menu_timestamps.items()
        if current_time - timestamp > 15  # 15 second timeout
    ]
    for username in stuck_players:
        players_in_game_menu.discard(username)
        game_menu_timestamps.pop(username, None)
        print(f"{username} auto-removed from game menu (timeout after 15 seconds)")
    
    # Debug: show how many players are in this tournament
    player_count = len(tournament.get('players', {}))
    player_names = list(tournament.get('players', {}).keys())
    print(f"Tournament {tournament_id[:8]}... has {player_count} players: {player_names}")
    
    pairings = lichess_style_pairing(tournament_id)
    print(f"Tournament {tournament_id[:8]}... pairing round: found {len(pairings)} pairings")
    
    for player1, player2 in pairings:
        # Double-check players are still available (not paused, not in game, not in game menu)
        if (player1 not in paused_users and player2 not in paused_users and
            player1 not in players_in_game_menu and player2 not in players_in_game_menu and
            not is_player_in_game(player1) and not is_player_in_game(player2)):
            print(f"Creating tournament game: {player1} vs {player2}")
            create_tournament_game(tournament_id, player1, player2)

def run_continuous_pairing():
    """Background thread that continuously pairs players in active tournaments"""
    while True:
        time.sleep(3)  # Check every 3 seconds
        try:
            # IMPORTANT: Activate any scheduled tournaments whose start time has passed
            # This ensures tournaments transition from 'scheduled' to 'active'
            start_scheduled_tournaments()
            
            for tournament_id, tournament in list(tournaments.items()):
                if tournament.get('status') == 'active':
                    run_tournament_pairing_round(tournament_id)
        except Exception as e:
            print(f"Error in continuous pairing: {e}")

# Pairing thread will be started when app runs

def match_tournament_players(tournament_id, username):
    """Match a specific player in tournament using Lichess-style pairing"""
    tournament = tournaments.get(tournament_id)
    if not tournament or tournament['status'] != 'active':
        return
    
    # Check if player is paused, in game, or in game menu
    if username in paused_users or username in players_in_game_menu or is_player_in_game(username):
        return
    
    # Find available opponents (not paused, not in game, not in game menu)
    available_opponents = []
    for player_name, player_data in tournament['players'].items():
        if (player_name != username and 
            player_name not in paused_users and
            player_name not in players_in_game_menu and
            not is_player_in_game(player_name)):
            available_opponents.append({
                'username': player_name,
                'score': player_data.get('score', 0),
                'rating': player_data.get('rating', 100)
            })
    
    if not available_opponents:
        return
    
    # Get current player's score
    current_player_data = tournament['players'].get(username, {})
    current_score = current_player_data.get('score', 0)
    
    # Sort opponents by score difference (prefer similar scores - Swiss pairing)
    available_opponents.sort(key=lambda x: abs(x['score'] - current_score))
    
    # Get last opponent to avoid (NEVER pair with last opponent)
    last_opponent = get_last_opponent(tournament_id, username)
    
    # Find best opponent (closest score, NOT the last opponent)
    best_opponent = None
    for opponent in available_opponents:
        # NEVER pair with last opponent
        if opponent['username'] == last_opponent:
            continue
        # Also check if we are the opponent's last opponent
        opponent_last = get_last_opponent(tournament_id, opponent['username'])
        if opponent_last == username:
            continue
        best_opponent = opponent['username']
        break
    
    # NO FALLBACK - player must wait if no valid opponent available
    if best_opponent:
        create_tournament_game(tournament_id, username, best_opponent)

def is_player_in_game(username):
    """Check if player is currently in a game"""
    for game_data in games.values():
        if username in [game_data.get('white'), game_data.get('black')] and game_data.get('status') != 'finished':
            return True
    return False

# API Routes
@app.route('/api/tournaments')
def api_tournaments():
    create_scheduled_tournaments()
    start_scheduled_tournaments()
    active_tournaments = [t for t in tournaments.values() if t['status'] == 'active']
    return jsonify({
        'server_now': datetime.now().isoformat(),
        'tournaments': active_tournaments
    })

@app.route('/api/leaderboard/<rating_type>')
def api_leaderboard(rating_type):
    if rating_type not in ['bullet', 'blitz']:
        return jsonify([])

    # Get top 3 rankings for badges
    bullet_top3, blitz_top3 = get_leaderboard_rankings()

    # Get all users sorted by rating
    user_list = []
    for username, user_data in users.items():
        if username in banned_users:
            continue
        title, title_color = get_title(user_data.get(f'{rating_type}_rating', 100), user_data)
        
        # Build ranking badges
        ranking_badges = []
        if username in bullet_top3:
            ranking_badges.append(f'B{bullet_top3[username]}')
        if username in blitz_top3:
            ranking_badges.append(f'R{blitz_top3[username]}')
        
        # Get ranking color for leaderboard display
        ranking_color = get_ranking_color(username)
        
        user_list.append({
            'username': username,
            'rating': user_data.get(f'{rating_type}_rating', 100),
            'games_played': user_data.get('games_played', {}).get(rating_type, 0),
            'wins': user_data.get('wins', {}).get(rating_type, 0),
            'losses': user_data.get('losses', {}).get(rating_type, 0),
            'draws': user_data.get('draws', {}).get(rating_type, 0),
            'color': ranking_color or user_data.get('color', '#c9c9c9'),
            'title': title or '',
            'title_color': title_color,
            'admin_rank': user_data.get('admin_rank'),
            'ranking_badges': ranking_badges
        })

    # Sort by rating (descending), then by games played (descending) to prioritize active players
    user_list.sort(key=lambda x: (x['rating'], x['games_played']), reverse=True)
    return jsonify(user_list[:100])  # Top 100 players

@app.route('/leaderboard')
def leaderboard_page():
    """Dedicated leaderboard page with full rankings"""
    if 'username' not in session:
        return redirect(url_for('login'))
    
    return render_template('leaderboard.html', username=session['username'])

@app.route('/api/search_players')
def api_search_players():
    query = request.args.get('q', '').lower().strip()
    if len(query) < 2:
        return jsonify([])

    matching_players = []
    for username, user_data in users.items():
        if query in username.lower():
            title, title_color = get_title(max(user_data.get('bullet_rating', 1200), user_data.get('blitz_rating', 1200)), user_data)
            matching_players.append({
                'username': username,
                'bullet_rating': user_data.get('bullet_rating', 1200),
                'blitz_rating': user_data.get('blitz_rating', 1200),
                'color': user_data.get('color', '#c9c9c9'),
                'title': title or '',
                'title_color': title_color,
                'is_banned': username in banned_users
            })

    # Sort by highest rating
    matching_players.sort(key=lambda x: max(x['bullet_rating'], x['blitz_rating']), reverse=True)
    return jsonify(matching_players[:10])  # Limit to 10 results

@app.route('/api/online_users')
def api_online_users():
    """Get list of currently online usernames"""
    unique_online = list(set(online_users.values()))
    return jsonify(unique_online)

@app.route('/api/is_online/<username>')
def api_is_online(username):
    """Check if a specific user is online"""
    is_online = username in set(online_users.values())
    return jsonify({'username': username, 'online': is_online})

@app.route('/api/banlist')
def api_banlist():
    """Get banlist data - requires Dragon+ rank"""
    username = session.get('username')
    if not username:
        return jsonify({'error': 'Not logged in'}), 401
    user_data = users.get(username, {})
    admin_rank = user_data.get('admin_rank')
    if admin_rank not in ['dragon', 'galaxy', 'creator']:
        return jsonify({'error': 'Insufficient rank'}), 403
    
    all_bans = BanRecord.query.order_by(BanRecord.id.desc()).all()
    recent_bans = [b.to_dict() for b in all_bans]
    currently_banned = [b.to_dict() for b in all_bans if b.is_active]
    
    return jsonify({
        'recent_bans': recent_bans,
        'banned_players': currently_banned
    })

@app.route('/api/user/<username>/games')
def api_user_games(username):
    user_games = []
    for game_id, game in games.items():
        if game.get('white') == username or game.get('black') == username:
            # Include timer data and opponent
            opponent = game['black'] if game['white'] == username else game['white']
            user_games.append({
                'id': game_id,
                'white': game.get('white'),
                'black': game.get('black'),
                'opponent': opponent,
                'result': game.get('winner', 'U tijeku'),
                'end_reason': game.get('end_reason', 'normal'),
                'date': game.get('start_time', datetime.now().strftime('%Y-%m-%d')),
                'time_control': game.get('time_control', '3+2')
            })

    return jsonify(sorted(user_games, key=lambda x: x['date'], reverse=True))

@app.route('/api/user/<username>/all-games')
def api_user_all_games(username):
    user_games = []
    for game_id, game in games.items():
        if (game.get('white') == username or game.get('black') == username) and game.get('status') == 'finished':
            # Determine result from user's perspective
            winner = game.get('winner')
            if winner == 'draw':
                result_text = 'draw'
            elif winner == username:
                result_text = 'win' 
            elif winner and winner != username:
                result_text = 'loss'
            else:
                result_text = 'unknown'

            # Format date nicely
            start_time = game.get('start_time', '')
            if start_time:
                try:
                    date_obj = datetime.fromisoformat(start_time)
                    formatted_date = date_obj.strftime('%Y-%m-%d %H:%M')
                except:
                    formatted_date = start_time[:10]
            else:
                formatted_date = 'Unknown'

            # Get rating change for this user
            rating_changes = game.get('rating_changes', {})
            user_rating_change = rating_changes.get(username, 0)

            white_user = users.get(game.get('white'), {})
            black_user = users.get(game.get('black'), {})
            user_games.append({
                'id': game_id,
                'white': game.get('white'),
                'black': game.get('black'),
                'white_admin_rank': white_user.get('admin_rank'),
                'black_admin_rank': black_user.get('admin_rank'),
                'white_color': white_user.get('color', '#c9c9c9'),
                'black_color': black_user.get('color', '#c9c9c9'),
                'winner': winner,
                'result': result_text,
                'end_reason': game.get('end_reason', 'normal'),
                'date': formatted_date,
                'time_control': game.get('time_control', '3+2'),
                'rating_changes': rating_changes,
                'user_rating_change': user_rating_change,
                'move_count': len(game.get('moves', [])),
                'tournament_id': game.get('tournament_id')
            })

    # Sort by date (most recent first)
    return jsonify(sorted(user_games, key=lambda x: x['date'], reverse=True))

@app.route('/api/live-game')
def api_live_game():
    best_game = get_best_live_game()
    return jsonify(best_game)

def update_tournament_scores(game_data, winner):
    """Update tournament scores after game
    
    Scoring system (Lichess-style):
    - Normal win = 2 points
    - Win with Berserk = 3 points
    - Win with win streak (3+ consecutive wins) = 4 points
    - Win with Berserk + win streak = 5 points
    - Draw = 1 point (unchanged with berserk)
    """
    tournament_id = game_data.get('tournament_id')
    if not tournament_id or tournament_id not in tournaments:
        return
    
    # Skip if tournament points are disabled (tournament ended while game was in progress)
    if game_data.get('tournament_points_disabled'):
        print(f"Skipping tournament points - tournament ended while game was in progress")
        return

    tournament = tournaments[tournament_id]
    white_player = game_data['white']
    black_player = game_data['black']

    # Ensure streak tracking exists for all players
    if 'streaks' not in tournament:
        tournament['streaks'] = {}
    if white_player not in tournament['streaks']:
        tournament['streaks'][white_player] = 0
    if black_player not in tournament['streaks']:
        tournament['streaks'][black_player] = 0

    # Update games played
    if white_player in tournament['players']:
        tournament['players'][white_player]['games_played'] += 1
    if black_player in tournament['players']:
        tournament['players'][black_player]['games_played'] += 1

    # Get current streaks before updating
    white_streak = tournament['streaks'].get(white_player, 0)
    black_streak = tournament['streaks'].get(black_player, 0)
    
    # Get berserk status
    white_berserk = game_data.get('berserk', {}).get('white', False)
    black_berserk = game_data.get('berserk', {}).get('black', False)

    # Ensure series tracking exists
    for player in [white_player, black_player]:
        if player in tournament['players'] and 'series' not in tournament['players'][player]:
            tournament['players'][player]['series'] = []

    # Get the rating type for this tournament
    rating_type = get_rating_type(tournament.get('time_control', '3+2'))
    
    # Get current ratings at game time (before any changes)
    white_user = users.get(white_player, {})
    black_user = users.get(black_player, {})
    white_rating_at_game = white_user.get(f'{rating_type}_rating', 1500)
    black_rating_at_game = black_user.get(f'{rating_type}_rating', 1500)

    if winner == 'white':
        # White wins
        if white_player in tournament['players']:
            # Increment white's streak first (for this win)
            tournament['streaks'][white_player] = white_streak + 1
            new_streak = tournament['streaks'][white_player]
            has_streak = new_streak >= 3  # Streak bonus only after 3+ consecutive wins
            
            # Calculate points: base 2, +1 for berserk, +2 for streak (or +3 for both)
            if white_berserk and has_streak:
                points = 5  # Berserk + streak
            elif has_streak:
                points = 4  # Streak only
            elif white_berserk:
                points = 3  # Berserk only
            else:
                points = 2  # Normal win
            
            tournament['players'][white_player]['score'] += points
            tournament['players'][white_player]['wins'].append({
                'opponent': black_player,
                'opponent_rating': black_rating_at_game,
                'date': datetime.now().isoformat(),
                'berserk': white_berserk,
                'points': points,
                'streak': new_streak,
                'game_id': game_data.get('id'),
                'color': 'white'
            })
            # Add to visual series: win = green (normal) or orange (streak)
            tournament['players'][white_player]['series'].append({
                'points': points,
                'type': 'win',
                'streak': has_streak,
                'berserk': white_berserk
            })
        
        # Black loses - reset streak
        if black_player in tournament['players']:
            tournament['streaks'][black_player] = 0
            tournament['players'][black_player]['losses'].append({
                'opponent': white_player,
                'opponent_rating': white_rating_at_game,
                'date': datetime.now().isoformat(),
                'berserk': black_berserk,
                'game_id': game_data.get('id'),
                'color': 'black'
            })
            # Add to visual series: loss = red, 0 points
            tournament['players'][black_player]['series'].append({
                'points': 0,
                'type': 'loss',
                'streak': False,
                'berserk': black_berserk
            })
            
    elif winner == 'black':
        # Black wins
        if black_player in tournament['players']:
            # Increment black's streak first (for this win)
            tournament['streaks'][black_player] = black_streak + 1
            new_streak = tournament['streaks'][black_player]
            has_streak = new_streak >= 3  # Streak bonus only after 3+ consecutive wins
            
            # Calculate points: base 2, +1 for berserk, +2 for streak (or +3 for both)
            if black_berserk and has_streak:
                points = 5  # Berserk + streak
            elif has_streak:
                points = 4  # Streak only
            elif black_berserk:
                points = 3  # Berserk only
            else:
                points = 2  # Normal win
            
            tournament['players'][black_player]['score'] += points
            tournament['players'][black_player]['wins'].append({
                'opponent': white_player,
                'opponent_rating': white_rating_at_game,
                'date': datetime.now().isoformat(),
                'berserk': black_berserk,
                'points': points,
                'streak': new_streak,
                'game_id': game_data.get('id'),
                'color': 'black'
            })
            # Add to visual series: win = green (normal) or orange (streak)
            tournament['players'][black_player]['series'].append({
                'points': points,
                'type': 'win',
                'streak': has_streak,
                'berserk': black_berserk
            })
        
        # White loses - reset streak
        if white_player in tournament['players']:
            tournament['streaks'][white_player] = 0
            tournament['players'][white_player]['losses'].append({
                'opponent': black_player,
                'opponent_rating': black_rating_at_game,
                'date': datetime.now().isoformat(),
                'berserk': white_berserk,
                'game_id': game_data.get('id'),
                'color': 'white'
            })
            # Add to visual series: loss = red, 0 points
            tournament['players'][white_player]['series'].append({
                'points': 0,
                'type': 'loss',
                'streak': False,
                'berserk': white_berserk
            })
            
    else:  # Draw
        # Draw awards points based on streak, then resets both streaks
        if white_player in tournament['players']:
            has_streak = white_streak >= 3
            points = 2 if has_streak else 1  # Draw with streak = 2, normal draw = 1
            tournament['players'][white_player]['score'] += points
            tournament['players'][white_player]['draws'].append({
                'opponent': black_player,
                'opponent_rating': black_rating_at_game,
                'date': datetime.now().isoformat(),
                'berserk': white_berserk,
                'points': points,
                'streak': white_streak,
                'game_id': game_data.get('id'),
                'color': 'white'
            })
            # Add to visual series: draw = white color
            tournament['players'][white_player]['series'].append({
                'points': points,
                'type': 'draw',
                'streak': has_streak,
                'berserk': white_berserk
            })
            # Reset streak on draw
            tournament['streaks'][white_player] = 0
            
        if black_player in tournament['players']:
            has_streak = black_streak >= 3
            points = 2 if has_streak else 1  # Draw with streak = 2, normal draw = 1
            tournament['players'][black_player]['score'] += points
            tournament['players'][black_player]['draws'].append({
                'opponent': white_player,
                'opponent_rating': white_rating_at_game,
                'date': datetime.now().isoformat(),
                'berserk': black_berserk,
                'points': points,
                'streak': black_streak,
                'game_id': game_data.get('id'),
                'color': 'black'
            })
            # Add to visual series: draw = white color
            tournament['players'][black_player]['series'].append({
                'points': points,
                'type': 'draw',
                'streak': has_streak,
                'berserk': black_berserk
            })
            # Reset streak on draw
            tournament['streaks'][black_player] = 0

@app.route('/api/join_tournament', methods=['POST'])
def api_join_tournament():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.get_json()
    tournament_id = data.get('tournament_id')
    username = session['username']

    if tournament_id in tournaments:
        tournament = tournaments[tournament_id]
        user = users.get(username)

        if user:
            rating_type = get_rating_type(tournament.get('time_control', '3+2'))
            user_rating = user.get(f'{rating_type}_rating', 100)

            tournament['players'][username] = {
                'score': 0,
                'games_played': 0,
                'rating': user_rating,
                'wins': [],
                'losses': [],
                'draws': [],
                'series': []  # Visual series: list of {points, type, streak} for display
            }
            return jsonify({'success': True})

    return jsonify({'error': 'Tournament not found'}), 404

@app.route('/api/leave_tournament', methods=['POST'])
def api_leave_tournament():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.get_json()
    tournament_id = data.get('tournament_id')
    username = session['username']

    if tournament_id in tournaments:
        tournament = tournaments[tournament_id]
        if username in tournament['players']:
            del tournament['players'][username]
            return jsonify({'success': True})

    return jsonify({'error': 'Tournament not found'}), 404

@app.route('/api/tournament_chat', methods=['POST'])
def api_tournament_chat():
    """HTTP endpoint for sending tournament chat messages (more reliable than WebSocket)"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    data = request.get_json()
    tournament_id = data.get('tournament_id')
    message = data.get('message', '').strip()
    
    print(f"[HTTP CHAT] Tournament chat from {username}: {message[:50]}...")
    
    if not tournament_id or not message:
        return jsonify({'error': 'Missing tournament_id or message'}), 400
    
    if tournament_id not in tournaments:
        return jsonify({'error': 'Tournament not found'}), 404
    
    tournament = tournaments[tournament_id]
    user_data = users.get(username, {})
    is_admin = user_data.get('admin_rank') is not None
    
    if username not in tournament['players'] and not is_admin:
        return jsonify({'error': 'Not in tournament'}), 403
    
    if tournament_id not in tournament_chats:
        tournament_chats[tournament_id] = []
    
    # Get rating based on tournament time control
    rating_type = get_rating_type(tournament.get('time_control', '3+2'))
    user_rating = user_data.get(f'{rating_type}_rating', 100)
    title, title_color = get_title(max(user_data.get('bullet_rating', 100), user_data.get('blitz_rating', 100)), user_data)
    
    chat_msg = {
        'username': username,
        'message': message[:200],
        'timestamp': datetime.now().isoformat(),
        'admin_rank': user_data.get('admin_rank'),
        'color': user_data.get('color', '#c9c9c9'),
        'title': title,
        'title_color': title_color,
        'rating': user_rating
    }
    tournament_chats[tournament_id].append(chat_msg)
    
    if len(tournament_chats[tournament_id]) > 100:
        tournament_chats[tournament_id] = tournament_chats[tournament_id][-100:]
    
    print(f"[HTTP CHAT] Broadcasting to all clients...")
    socketio.emit('tournament_chat_update', {
        'tournament_id': tournament_id,
        'message': chat_msg
    })
    
    return jsonify({'success': True, 'message': chat_msg})

@app.route('/api/tournament_chat/<tournament_id>', methods=['GET'])
def api_get_tournament_chat(tournament_id):
    """Get tournament chat history via HTTP"""
    messages = tournament_chats.get(tournament_id, [])
    return jsonify({'tournament_id': tournament_id, 'messages': messages})

@app.route('/api/player/<username>/hover')
def api_player_hover(username):
    """Return player hover card data: ratings, title, and current live game if any"""
    user_data = users.get(username, {})
    if not user_data:
        return jsonify({'error': 'Player not found'}), 404
    
    # Get all ratings
    bullet_rating = user_data.get('bullet_rating', 100)
    blitz_rating = user_data.get('blitz_rating', 100)
    color = user_data.get('color', '#c9c9c9')
    title, title_color = get_title(max(bullet_rating, blitz_rating), user_data)
    
    # Check for any current live game (not just tournament)
    current_game = None
    for game_id, game in games.items():
        game_status = game.get('status', '')
        is_active = game_status not in ['finished', 'abandoned', '']
        if is_active and (game.get('white') == username or game.get('black') == username):
            opponent = game.get('black') if game.get('white') == username else game.get('white')
            opponent_data = users.get(opponent, {})
            player_color = 'white' if game.get('white') == username else 'black'
            current_game = {
                'game_id': game_id,
                'opponent': opponent,
                'opponent_color': opponent_data.get('color', '#c9c9c9'),
                'player_color': player_color,
                'board': game.get('board', [0] * 24),
                'white': game.get('white'),
                'black': game.get('black'),
                'white_color': users.get(game.get('white'), {}).get('color', '#c9c9c9'),
                'black_color': users.get(game.get('black'), {}).get('color', '#c9c9c9'),
                'tournament_id': game.get('tournament_id')
            }
            break
    
    return jsonify({
        'username': username,
        'bullet_rating': bullet_rating,
        'blitz_rating': blitz_rating,
        'color': color,
        'title': title or '',
        'title_color': title_color,
        'current_game': current_game
    })

@app.route('/api/player/<username>/tournament/<tournament_id>/stats')
def api_player_tournament_stats(username, tournament_id):
    if tournament_id not in tournaments:
        return jsonify({'error': 'Tournament not found'}), 404

    tournament = tournaments[tournament_id]
    if username not in tournament['players']:
        return jsonify({'error': 'Player not in tournament'}), 404

    player_data = tournament['players'][username]
    user_data = users.get(username, {})
    
    # Check if player is currently in a live game for this tournament
    current_game = None
    for game_id, game in games.items():
        # Check for any active game status (waiting_first_move, playing, etc - not finished)
        game_status = game.get('status', '')
        is_active = game_status not in ['finished', 'abandoned', '']
        if game.get('tournament_id') == tournament_id and is_active:
            # Use 'white' and 'black' keys (not white_player/black_player)
            if game.get('white') == username or game.get('black') == username:
                opponent = game.get('black') if game.get('white') == username else game.get('white')
                opponent_data = users.get(opponent, {})
                player_color = 'white' if game.get('white') == username else 'black'
                # Get opponent rating based on time control
                tc = game.get('time_control', '3+2')
                rating_type = get_rating_type(tc)
                opponent_rating = opponent_data.get(f'{rating_type}_rating', 1500)
                current_game = {
                    'game_id': game_id,
                    'opponent': opponent,
                    'opponent_color': opponent_data.get('color', '#c9c9c9'),
                    'opponent_rating': opponent_rating,
                    'color': player_color,
                    'board': game.get('board', [0] * 24),
                    'white_time': game.get('white_time', 180),
                    'black_time': game.get('black_time', 180),
                    'current_turn': game.get('current_turn', 'white')
                }
                break

    # Enrich game history with opponent colors (opponent_rating is already stored in game history)
    rating_type = get_rating_type(tournament.get('time_control', '3+2'))
    
    def enrich_games(games_list):
        enriched = []
        for game in games_list:
            opponent = game.get('opponent', '')
            opponent_data = users.get(opponent, {})
            opponent_color = opponent_data.get('color', '#c9c9c9')
            # Use stored opponent_rating from game time, fallback to current rating for old games
            stored_rating = game.get('opponent_rating')
            if stored_rating is None:
                stored_rating = opponent_data.get(f'{rating_type}_rating', 1500)
            enriched.append({
                **game,
                'opponent_rating': stored_rating,
                'opponent_color': opponent_color
            })
        return enriched
    
    # Get player's CURRENT rating (not the rating at join time)
    current_rating = user_data.get(f'{rating_type}_rating', 1500)
    
    # Calculate berserk stats
    all_games = player_data.get('wins', []) + player_data.get('losses', []) + player_data.get('draws', [])
    berserk_count = sum(1 for g in all_games if g.get('berserk', False))
    total_games = len(all_games)
    berserk_rate = round((berserk_count / total_games * 100) if total_games > 0 else 0)
    
    # Get ranking badges [B1], [R1], etc.
    ranking_badges = get_ranking_badge(username)
    
    return jsonify({
        'username': username,
        'score': player_data.get('score', 0),
        'games_played': player_data.get('games_played', 0),
        'wins': enrich_games(player_data.get('wins', [])),
        'losses': enrich_games(player_data.get('losses', [])),
        'draws': enrich_games(player_data.get('draws', [])),
        'rating': current_rating,  # Use current rating, not join-time rating
        'color': user_data.get('color', '#c9c9c9'),
        'title': get_title(max(user_data.get('bullet_rating', 100), user_data.get('blitz_rating', 100)), user_data),
        'current_game': current_game,
        'berserk_count': berserk_count,
        'berserk_rate': berserk_rate,
        'admin_rank': user_data.get('admin_rank'),
        'ranking_badges': ranking_badges
    })

# Additional Socket.IO events
@socketio.on('join_tournament')
def on_join_tournament(data):
    username = session.get('username')
    tournament_id = data['tournament_id']
    print(f"Join tournament request: user={username}, tournament={tournament_id[:8]}...")

    if username and tournament_id in tournaments:
        tournament = tournaments[tournament_id]
        user = users.get(username)
        
        # Check if this is an admin-only tournament
        if tournament.get('admin_only'):
            user_rank = user.get('admin_rank') if user else None
            is_invited = username in tournament.get('invited_users', [])
            is_admin = user_rank in ['admin', 'dragon', 'galaxy', 'creator']
            
            if not is_admin and not is_invited:
                emit('tournament_join_error', {'error': 'This is an admin-only tournament. You need to be invited to join.'})
                return
        
        # Clear player from any blocking states so they can be paired immediately
        players_in_game_menu.discard(username)
        paused_users.discard(username)
        
        # CRITICAL FIX: If tournament is scheduled but start time has passed, activate it now
        if tournament['status'] == 'scheduled':
            start_time = datetime.fromisoformat(tournament['start_time'])
            current_time = datetime.now()
            print(f"Tournament {tournament_id[:8]}... join check: now={current_time.isoformat()}, start={tournament['start_time']}, diff={(current_time - start_time).total_seconds()}s")
            if current_time >= start_time:
                tournament['status'] = 'active'
                print(f"Tournament {tournament_id[:8]}... activated on join (start_time passed)")
            else:
                print(f"Tournament {tournament_id[:8]}... NOT activated (starts in {(start_time - current_time).total_seconds():.0f}s)")

        if user:
            rating_type = get_rating_type(tournament.get('time_control', '3+2'))
            user_rating = user.get(f'{rating_type}_rating', 100)

            tournament['players'][username] = {
                'score': 0,
                'games_played': 0,
                'rating': user_rating,
                'wins': [],
                'losses': [],
                'draws': [],
                'berserk': False,
                'series': []  # Visual series for Lichess-style display
            }
            print(f"Player {username} joined tournament {tournament_id[:8]}... Total players: {len(tournament['players'])} Status: {tournament['status']}")
            emit('tournament_joined', {'tournament_id': tournament_id})
            
            # Broadcast to ALL clients that leaderboard changed (not just room members)
            # This fixes the issue where the second player in admin-created tournaments wasn't showing
            socketio.emit('tournament_player_joined', {
                'tournament_id': tournament_id,
                'username': username,
                'player_count': len(tournament['players'])
            })

            # Delay pairing to allow the player to see themselves on leaderboard first
            # The player will request pairing after their leaderboard updates
            # Only do immediate matching if there's already someone waiting
            if tournament['status'] == 'active' and username not in paused_users:
                # Delay pairing by 1.5 seconds so player can see themselves on leaderboard
                def delayed_match():
                    if tournament_id in tournaments and username in tournaments[tournament_id]['players']:
                        if username not in paused_users and username not in players_in_game_menu:
                            print(f"Delayed match: trying to pair {username} after leaderboard update...")
                            match_tournament_players(tournament_id, username)
                
                timer = threading.Timer(1.5, delayed_match)
                timer.daemon = True
                timer.start()
                print(f"Tournament is active, delayed pairing for {username} by 1.5s to allow leaderboard update")
    else:
        print(f"Join tournament failed: username={username}, tournament_id exists={tournament_id in tournaments}")

@socketio.on('leave_tournament')
def on_leave_tournament(data):
    username = session.get('username')
    tournament_id = data['tournament_id']

    if username and tournament_id in tournaments:
        tournament = tournaments[tournament_id]
        if username in tournament['players']:
            del tournament['players'][username]
            emit('tournament_left', {'tournament_id': tournament_id})

@socketio.on('tournament_chat_message')
def on_tournament_chat_message(data):
    username = session.get('username')
    tournament_id = data.get('tournament_id')
    message = data.get('message', '').strip()
    
    print(f"[CHAT] Tournament chat message from {username}: {message[:50]}...")
    
    if not username or not tournament_id or not message:
        print(f"[CHAT] Missing data: username={username}, tournament_id={tournament_id}, message={bool(message)}")
        return
    
    if tournament_id not in tournaments:
        print(f"[CHAT] Tournament {tournament_id[:8]}... not found")
        return
    
    # Check if user is in the tournament (allow admins to chat even if not joined)
    tournament = tournaments[tournament_id]
    user_data = users.get(username, {})
    is_admin = user_data.get('admin_rank') is not None
    if username not in tournament['players'] and not is_admin:
        print(f"[CHAT] User {username} not in tournament players and not admin")
        return
    
    # Initialize chat if needed
    if tournament_id not in tournament_chats:
        tournament_chats[tournament_id] = []
    
    # Add message
    chat_msg = {
        'username': username,
        'message': message[:200],  # Limit message length
        'timestamp': datetime.now().isoformat(),
        'admin_rank': user_data.get('admin_rank'),
        'color': user_data.get('color', '#c9c9c9')
    }
    tournament_chats[tournament_id].append(chat_msg)
    print(f"[CHAT] Message added to tournament {tournament_id[:8]}..., total messages: {len(tournament_chats[tournament_id])}")
    
    # Keep only last 100 messages
    if len(tournament_chats[tournament_id]) > 100:
        tournament_chats[tournament_id] = tournament_chats[tournament_id][-100:]
    
    # Broadcast to ALL connected clients (they filter by tournament_id on client side)
    # This is more reliable than room-based broadcasting
    print(f"[CHAT] Broadcasting to all clients for tournament {tournament_id[:8]}...")
    socketio.emit('tournament_chat_update', {
        'tournament_id': tournament_id,
        'message': chat_msg
    })
    print(f"[CHAT] Global broadcast sent")

@socketio.on('get_tournament_chat')
def on_get_tournament_chat(data):
    tournament_id = data.get('tournament_id')
    if tournament_id and tournament_id in tournament_chats:
        emit('tournament_chat_history', {
            'tournament_id': tournament_id,
            'messages': tournament_chats[tournament_id]
        })
    else:
        emit('tournament_chat_history', {
            'tournament_id': tournament_id,
            'messages': []
        })

@socketio.on('chat_message')
def on_chat_message(data):
    username = session.get('username')
    room_id = data['room_id']
    message = data['message']

    if username and room_id:
        emit('chat_message', {
            'username': username,
            'message': message,
            'timestamp': datetime.now().isoformat()
        }, room=room_id)

@socketio.on('join_game')
def on_join_game(data):
    room_id = data['room_id']
    username = session.get('username')

    if room_id not in games:
        emit('game_canceled', {
            'reason': 'Game not found - likely canceled',
            'message': 'This game has been canceled or does not exist',
            'redirect_to_lobby': True
        })
        return

    game_data = games[room_id]

    # Check if game was canceled
    if game_data.get('status') == 'canceled':
        emit('game_canceled', {
            'reason': 'Game was canceled',
            'message': 'This game has been canceled',
            'redirect_to_lobby': True
        })
        return

    # Check if game object is missing (finished/old game loaded from DB)
    if not game_data.get('game'):
        emit('game_canceled', {
            'reason': 'Game has ended',
            'message': 'This game has already finished',
            'redirect_to_lobby': True
        })
        return

    join_room(room_id)

    # Get user data for display
    white_user_data = users.get(game_data['white'], {})
    black_user_data = users.get(game_data['black'], {})

    # Get titles for both players - use max rating to ensure highest title is calculated
    white_max_rating = max(white_user_data.get('bullet_rating', 100), white_user_data.get('blitz_rating', 100))
    black_max_rating = max(black_user_data.get('bullet_rating', 100), black_user_data.get('blitz_rating', 100))
    white_title, white_title_color = get_title(white_max_rating, white_user_data)
    black_title, black_title_color = get_title(black_max_rating, black_user_data)

    # Add title info to user data - ALWAYS use highest title achieved
    white_highest = white_user_data.get('highest_title')
    white_display_title = white_highest if white_highest else white_title
    white_user_data['title'] = white_display_title or ''
    white_user_data['title_color'] = white_user_data.get('highest_title_color', white_title_color)
    black_highest = black_user_data.get('highest_title')
    black_display_title = black_highest if black_highest else black_title
    black_user_data['title'] = black_display_title or ''
    black_user_data['title_color'] = black_user_data.get('highest_title_color', black_title_color)

    # Get current timer info
    timer_info = get_timer_info(room_id)
    if not timer_info:
        timer_info = {
            'timers': game_data.get('timers', {'white': 0, 'black': 0}),
            'active_player': game_data['game'].current_player if game_data.get('game') else 'white',
            'server_time': time.time()
        }

    # Calculate current piece counts for rejoining player
    piece_counts = calculate_piece_counts(game_data)
    
    # Check if this is a tournament game waiting for first move
    is_tournament = game_data.get('tournament_id') is not None
    waiting_first_move = game_data.get('status') == 'waiting_first_move' and not game_data.get('first_move_made', False)
    first_move_countdown_remaining = 0
    
    if waiting_first_move:
        # Calculate remaining countdown time from server start time
        first_move_start = game_data.get('first_move_start_time') or game_data.get('game_started_at', time.time())
        elapsed = time.time() - first_move_start
        first_move_countdown_remaining = max(0, 20 - elapsed)

    # Determine if this is a spectator (not white or black)
    is_spectator = username not in [game_data['white'], game_data['black']]
    if is_spectator:
        print(f"Spectator {username} joined game {room_id}")
    
    # Get ranking badges for both players
    white_badges = get_ranking_badge(game_data['white'])
    black_badges = get_ranking_badge(game_data['black'])
    
    # Add ranking colors to user data
    white_user_data['ranking_color'] = get_ranking_color(game_data['white'])
    black_user_data['ranking_color'] = get_ranking_color(game_data['black'])
    
    # Get piece designs for both players
    white_piece_design = white_user_data.get('piece_design', 'classic')
    black_piece_design = black_user_data.get('piece_design', 'classic')
    
    emit('game_state', {
        'room_id': room_id,  # Include room_id for spectators
        'status': game_data.get('status', 'playing'),  # Include status for troll commands
        'board': game_data['game'].board,
        'current_player': game_data['game'].current_player,
        'phase': game_data['game'].phase,
        'white': game_data['white'],
        'black': game_data['black'],
        'white_rank': game_data.get('white_rank'),
        'black_rank': game_data.get('black_rank'),
        'white_badges': white_badges,
        'black_badges': black_badges,
        'time_control': game_data.get('time_control', '3+2'),
        'your_color': 'spectator' if is_spectator else ('white' if username == game_data['white'] else 'black'),
        'white_user_data': white_user_data,
        'black_user_data': black_user_data,
        'timer_info': timer_info,
        'moves': game_data.get('moves', []),  # Send existing moves
        'waiting_for_removal': game_data.get('waiting_for_removal', False),  # Restore mill removal state
        'piece_counts': piece_counts,  # Include current piece counts for rejoining player
        'is_tournament': is_tournament,
        'tournament_id': game_data.get('tournament_id'),
        'waiting_first_move': waiting_first_move,
        'first_move_countdown_remaining': first_move_countdown_remaining,
        'berserk': game_data.get('berserk', {'white': False, 'black': False}),
        'is_spectator': is_spectator,
        'piece_designs': {'white': white_piece_design, 'black': black_piece_design}
    })

@socketio.on('request_timer_sync')
def on_request_timer_sync(data):
    """Handle client request for timer synchronization"""
    room_id = data.get('room_id')
    username = session.get('username')

    if not room_id or not username:
        return

    game_data = games.get(room_id)
    if not game_data:
        return
    
    # Allow spectators to sync timers too (not just white/black players)
    timer_info = get_timer_info(room_id)
    if timer_info:
        emit('timer_sync', {
            'timers': timer_info['timers'],
            'active_player': timer_info['active_player'],
            'server_time': timer_info['server_time'],
            'full_sync': True
        })

@socketio.on('send_challenge')
def on_send_challenge(data):
    challenger = session.get('username')
    opponent = data.get('opponent')
    time_control = data.get('time_control', '3+2')
    game_type = data.get('game_type', 'ranked')

    if not challenger or not opponent or opponent not in users:
        emit('challenge_error', {'message': 'Invalid challenge request'})
        return

    if challenger == opponent:
        emit('challenge_error', {'message': 'Cannot challenge yourself'})
        return

    if challenger in banned_users:
        emit('challenge_error', {'message': 'You are banned and cannot send challenges'})
        return

    if opponent in banned_users:
        emit('challenge_error', {'message': 'Cannot challenge banned players'})
        return

    # Check if opponent is online
    opponent_online = any(user == opponent for user in online_users.values())
    if not opponent_online:
        emit('challenge_error', {'message': f'{opponent} is not online'})
        return

    # Check if either player has an active game
    if get_active_game_for_player(challenger) or get_active_game_for_player(opponent):
        emit('challenge_error', {'message': 'One of the players is already in a game'})
        return

    challenge_id = str(uuid.uuid4())

    # Store challenge data globally for later reference
    global challenges
    challenges[challenge_id] = {
        'challenger': challenger,
        'opponent': opponent,
        'time_control': time_control,
        'game_type': game_type,
        'created_at': datetime.now().isoformat()
    }

    # Find ALL opponent session IDs
    opponent_sids = []
    for sid, user_data in online_users.items():
        if user_data == opponent:
            opponent_sids.append(sid)

    print(f"Sending challenge from {challenger} to {opponent}, found {len(opponent_sids)} sessions for opponent")

    # Send challenge to all opponent sessions globally (not just in profile)
    for opponent_sid in opponent_sids:
        socketio.emit('challenge_received', {
            'challenger': challenger,
            'time_control': time_control,
            'game_type': game_type,
            'challenge_id': challenge_id,
            'global_notification': True  # Flag for global notification system
        }, to=opponent_sid)
        print(f"Sent challenge notification to session {opponent_sid}")

    emit('challenge_sent', {'message': f'Challenge sent to {opponent}'})

@socketio.on('accept_challenge')
def on_accept_challenge(data):
    username = session.get('username')
    challenge_id = data.get('challenge_id')

    if not username or not challenge_id:
        emit('challenge_error', {'message': 'Invalid challenge acceptance'})
        return

    # Get challenge data
    global challenges
    challenge = challenges.get(challenge_id)
    if not challenge:
        emit('challenge_error', {'message': 'Challenge not found or expired'})
        return

    if challenge['opponent'] != username:
        emit('challenge_error', {'message': 'You are not the target of this challenge'})
        return

    challenger = challenge['challenger']
    time_control = challenge['time_control']
    game_type = challenge['game_type']

    # Check if either player has an active game
    if get_active_game_for_player(challenger) or get_active_game_for_player(username):
        emit('challenge_error', {'message': 'One of the players is already in a game'})
        return

    # Create game
    # Randomly assign colors
    players = [challenger, username]
    random.shuffle(players)
    white_player = players[0]
    black_player = players[1]

    # Parse time control
    parts = time_control.split('+')
    minutes = int(parts[0])
    increment = int(parts[1]) if len(parts) > 1 else 0
    base_time = minutes * 60

    # Create game
    game = NineMensMorris()
    game_id = str(uuid.uuid4())
    games[game_id] = {
        'id': game_id,
        'white': white_player,
        'black': black_player,
        'game': game,
        'moves': [],
        'positions': [game.board[:]],
        'time_control': time_control,
        'start_time': datetime.now().isoformat(),
        'status': 'playing',
        'berserk': {'white': False, 'black': False},
        'timers': {'white': base_time, 'black': base_time},
        'increment': increment,
        'last_move_time': datetime.now(),
        'server_start_time': time.time(),
        'active_timer': 'white',
        'game_type': game_type  # Store game type for rating calculation
    }

    # Start server-authoritative timer
    start_game_timer(game_id)

    # Get session IDs for both players
    challenger_sids = []
    opponent_sids = []

    for sid, user_data in online_users.items():
        if user_data == challenger:
            challenger_sids.append(sid)
        elif user_data == username:
            opponent_sids.append(sid)

    # Add all sessions to game room
    for sid in challenger_sids + opponent_sids:
        join_room(game_id, sid=sid)

    # Get user data
    white_user_data = users.get(white_player, {})
    black_user_data = users.get(black_player, {})
    
    # Add ranking colors to user data
    white_user_data['ranking_color'] = get_ranking_color(white_player)
    black_user_data['ranking_color'] = get_ranking_color(black_player)

    # Calculate initial piece counts
    piece_counts = calculate_piece_counts(games[game_id])

    # Get ranking badges for both players
    white_badges = get_ranking_badge(white_player)
    black_badges = get_ranking_badge(black_player)
    
    # Prepare game data
    game_data = {
        'room_id': game_id,
        'white': white_player,
        'black': black_player,
        'time_control': time_control,
        'game_type': game_type,
        'white_user_data': white_user_data,
        'black_user_data': black_user_data,
        'piece_counts': piece_counts,
        'white_badges': white_badges,
        'black_badges': black_badges
    }

    # Send game start to challenger
    for sid in challenger_sids:
        socketio.emit('challenge_accepted', {
            **game_data,
            'your_color': 'white' if challenger == white_player else 'black'
        }, to=sid)
        socketio.emit('players_matched', {
            **game_data,
            'your_color': 'white' if challenger == white_player else 'black',
            'timers': {'white': base_time, 'black': base_time}
        }, to=sid)

    # Send game start to opponent
    for sid in opponent_sids:
        socketio.emit('challenge_accepted', {
            **game_data,
            'your_color': 'white' if username == white_player else 'black'
        }, to=sid)
        socketio.emit('players_matched', {
            **game_data,
            'your_color': 'white' if username == white_player else 'black',
            'timers': {'white': base_time, 'black': base_time}
        }, to=sid)

    # Clean up challenge
    del challenges[challenge_id]

    print(f"Challenge accepted: {challenger} vs {username} ({time_control}, {game_type})")

@socketio.on('decline_challenge')
def on_decline_challenge(data):
    username = session.get('username')
    challenge_id = data.get('challenge_id')

    if not username or not challenge_id:
        emit('challenge_error', {'message': 'Invalid challenge decline'})
        return

    # Get challenge data
    global challenges
    challenge = challenges.get(challenge_id)
    if not challenge:
        emit('challenge_error', {'message': 'Challenge not found or expired'})
        return

    if challenge['opponent'] != username:
        emit('challenge_error', {'message': 'You are not the target of this challenge'})
        return

    challenger = challenge['challenger']

    # Find challenger session IDs
    challenger_sids = []
    for sid, user_data in online_users.items():
        if user_data == challenger:
            challenger_sids.append(sid)

    # Send decline message to challenger
    for sid in challenger_sids:
        socketio.emit('challenge_declined', {
            'message': f'{username} declined your challenge',
            'declined_by': username
        }, to=sid)

    # Clean up challenge
    del challenges[challenge_id]

    emit('challenge_declined', {'message': 'Challenge declined'})

@socketio.on('berserk')
def on_berserk(data):
    """Handle berserk activation - halve time for chance at bonus point"""
    username = session.get('username')
    room_id = data.get('room_id')
    game_data = games.get(room_id)
    
    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return
    
    player_color = 'white' if username == game_data['white'] else 'black'
    
    # Can only berserk in first 2 moves of the game
    if len(game_data.get('moves', [])) > 2:
        emit('berserk_error', {'message': 'Too late to berserk'})
        return
    
    # Can only berserk once
    if game_data.get('berserk', {}).get(player_color, False):
        emit('berserk_error', {'message': 'Already berserked'})
        return
    
    # Must be a tournament game
    if not game_data.get('tournament_id'):
        emit('berserk_error', {'message': 'Berserk only available in tournaments'})
        return
    
    # Halve the player's time
    if 'berserk' not in game_data:
        game_data['berserk'] = {'white': False, 'black': False}
    
    game_data['berserk'][player_color] = True
    
    # Halve the remaining time
    if 'timers' in game_data:
        game_data['timers'][player_color] = game_data['timers'][player_color] / 2
    
    # Update the timer in game_timers if exists
    with timer_lock:
        if room_id in game_timers:
            timer = game_timers[room_id]
            if player_color == 'white':
                timer.white_time = timer.white_time / 2
            else:
                timer.black_time = timer.black_time / 2
    
    # Notify all players
    socketio.emit('berserk_activated', {
        'player': username,
        'color': player_color,
        'new_time': game_data['timers'].get(player_color, 0)
    }, room=room_id)
    
    print(f"{username} activated berserk in game {room_id}")

@socketio.on('offer_draw')
def on_offer_draw(data):
    username = session.get('username')
    room_id = data.get('room_id')
    player_color = data.get('player_color')
    game_data = games.get(room_id)

    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return

    opponent = game_data['black'] if username == game_data['white'] else game_data['white']

    # Find ALL opponent session IDs and send draw offer to each
    opponent_sids = []
    for sid, user_data in online_users.items():
        if user_data == opponent:
            opponent_sids.append(sid)

    # Send draw offer to all opponent sessions
    for opponent_sid in opponent_sids:
        socketio.emit('draw_offered', {'player': username}, to=opponent_sid)

    # Send confirmation to the player who offered the draw
    emit('draw_offer_sent', {'player_color': player_color})

@socketio.on('accept_draw')
def on_accept_draw(data):
    username = session.get('username')
    room_id = data.get('room_id')
    game_data = games.get(room_id)

    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return

    # End the game as a draw
    game_data['status'] = 'finished'
    game_data['winner'] = 'draw'
    game_data['end_reason'] = 'agreement'

    # Stop the timer
    stop_game_timer(room_id)

    # Update ratings and get the changes
    update_ratings(game_data, 'draw')

    # Get updated user data for both players
    white_user = users[game_data['white']]
    black_user = users[game_data['black']]
    rating_type = get_rating_type(game_data.get('time_control', '3+2'))

    # Send game over with complete rating information
    socketio.emit('game_over', {
        'winner': 'draw',
        'reason': 'agreement',
        'rating_changes': game_data.get('rating_changes', {}),
        'new_ratings': {
            game_data['white']: white_user[f'{rating_type}_rating'],
            game_data['black']: black_user[f'{rating_type}_rating']
        },
        'rating_type': rating_type
    }, room=room_id)

    update_tournament_scores(game_data, 'draw')
    
    # Re-queue players for next tournament game
    requeue_tournament_players(game_data)

@socketio.on('decline_draw')
def on_decline_draw(data):
    username = session.get('username')
    room_id = data.get('room_id')
    player_color = data.get('player_color')
    game_data = games.get(room_id)

    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return

    # Send decline message to the room
    socketio.emit('draw_declined', {
        'message': f'{username} declined the draw offer',
        'player_color': player_color
    }, room=room_id)

@socketio.on('resign')
def on_resign(data):
    username = session.get('username')
    room_id = data.get('room_id')
    game_data = games.get(room_id)

    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return
    
    # Prevent resigning an already finished game
    if game_data.get('status') == 'finished' or game_data.get('winner'):
        print(f"Resign rejected: game {room_id} already finished")
        return

    # Determine winner (opponent of the one who resigned)
    winner = 'black' if username == game_data['white'] else 'white'

    # End the game immediately
    game_data['status'] = 'finished'
    game_data['winner'] = winner
    game_data['end_reason'] = 'resignation'

    # Stop the timer
    stop_game_timer(room_id)

    # Update ratings and get the changes
    update_ratings(game_data, winner)

    # Get updated user data for both players
    white_user = users[game_data['white']]
    black_user = users[game_data['black']]
    rating_type = get_rating_type(game_data.get('time_control', '3+2'))

    # Send game over with complete rating information immediately
    socketio.emit('game_over', {
        'winner': winner,
        'reason': 'resignation',
        'rating_changes': game_data.get('rating_changes', {}),
        'new_ratings': {
            game_data['white']: white_user[f'{rating_type}_rating'],
            game_data['black']: black_user[f'{rating_type}_rating']
        },
        'rating_type': rating_type
    }, room=room_id)

    update_tournament_scores(game_data, winner)
    
    # Re-queue players for next tournament game
    requeue_tournament_players(game_data)

    print(f"Game {room_id} ended by resignation - {username} resigned, {winner} wins")

# Removed client-side time_up handler - server now handles all timeout logic

# In-game troll commands - meme attacks
TROLL_MEMES = {
    'shocked': {'name': 'Shocked Black Guy', 'image': 'shocked.jpg', 'duration': 1000, 'sound': None},
    'skeleton': {'name': 'Skeleton Shield Bang', 'image': 'skeleton.jpg', 'duration': 3000, 'sound': 'bang.mp3'},
    'praying': {'name': 'Guy Praying', 'image': 'praying.jpg', 'duration': 2000, 'sound': 'fahh.mp3'},
    'explosion': {'name': 'Explosion', 'image': 'explosion.jpg', 'duration': 3000, 'sound': 'explosion.mp3'},
    'confused': {'name': 'Confused Dirigent', 'image': 'confused.jpg', 'duration': 2000, 'sound': 'fahh.mp3'},
    'pointing': {'name': 'Are You Pointing At Me', 'image': 'pointing.jpg', 'duration': 2000, 'sound': 'gunshot.mp3'}
}

@socketio.on('troll_kick')
def on_troll_kick(data):
    """Kick opponent from game temporarily with shocked meme"""
    username = session.get('username')
    room_id = data.get('room_id')
    kick_duration = max(1, min(int(data.get('duration', 5)), 10))  # 1-10 seconds
    game_data = games.get(room_id)
    
    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return
    
    # Only admins and higher ranks can use troll commands
    user_data = users.get(username, {})
    admin_rank = user_data.get('admin_rank')
    if admin_rank not in ['admin', 'dragon', 'galaxy', 'creator']:
        emit('troll_error', {'message': 'Only admins can use troll commands'})
        return
    
    # Only allow during active game (status is 'playing' or 'waiting_first_move')
    if game_data.get('status') not in ['playing', 'waiting_first_move']:
        return
    
    # Find opponent
    opponent = game_data['black'] if username == game_data['white'] else game_data['white']
    
    # Check if opponent has same or higher admin rank
    opponent_data = users.get(opponent, {})
    opponent_rank = opponent_data.get('admin_rank')
    if opponent_rank and get_admin_rank_level(opponent_rank) >= get_admin_rank_level(admin_rank):
        emit('troll_error', {'message': f'Cannot kick {opponent} - same or higher rank'})
        return
    
    # Send kick effect to opponent
    for sid, user in online_users.items():
        if user == opponent:
            socketio.emit('troll_kick_received', {
                'kicker': username,
                'duration': kick_duration * 1000,
                'meme': 'shocked'
            }, to=sid)
    
    # Send success message to sender
    emit('troll_success', {'message': f'Kick successful! {opponent} kicked for {kick_duration}s', 'type': 'kick', 'target': opponent, 'duration': kick_duration})

@socketio.on('troll_meme')
def on_troll_meme(data):
    """Send a meme popup to opponent"""
    username = session.get('username')
    room_id = data.get('room_id')
    meme_type = data.get('meme_type', 'shocked')
    game_data = games.get(room_id)
    
    if not game_data or username not in [game_data.get('white'), game_data.get('black')]:
        return
    
    # Only admins and higher ranks can use troll commands
    user_data = users.get(username, {})
    admin_rank = user_data.get('admin_rank')
    if admin_rank not in ['admin', 'dragon', 'galaxy', 'creator']:
        emit('troll_error', {'message': 'Only admins can use troll commands'})
        return
    
    # Only allow during active game (status is 'playing' or 'waiting_first_move')
    if game_data.get('status') not in ['playing', 'waiting_first_move']:
        return
    
    # Validate meme type
    if meme_type not in TROLL_MEMES:
        return
    
    meme_info = TROLL_MEMES[meme_type]
    
    # Find opponent
    opponent = game_data['black'] if username == game_data['white'] else game_data['white']
    
    # Check if opponent has same or higher admin rank
    opponent_data = users.get(opponent, {})
    opponent_rank = opponent_data.get('admin_rank')
    if opponent_rank and get_admin_rank_level(opponent_rank) >= get_admin_rank_level(admin_rank):
        emit('troll_error', {'message': f'Cannot jumpscare {opponent} - same or higher rank'})
        return
    
    # Send meme to opponent
    for sid, user in online_users.items():
        if user == opponent:
            socketio.emit('troll_meme_received', {
                'sender': username,
                'meme_type': meme_type,
                'meme_info': meme_info
            }, to=sid)
    
    # Send success message to sender
    emit('troll_success', {'message': 'Jumpscare successful!', 'type': 'meme', 'target': opponent, 'meme': meme_type})

@socketio.on('request_rematch')
def on_request_rematch(data):
    username = session.get('username')
    room_id = data['room_id']
    game_data = games.get(room_id)

    if not game_data or username not in [game_data['white'], game_data['black']]:
        return

    # Check if rematch already in progress
    if game_data.get('rematch_in_progress'):
        return

    # Store rematch request
    if 'rematch_requests' not in game_data:
        game_data['rematch_requests'] = set()

    game_data['rematch_requests'].add(username)

    # Notify the opponent about the rematch offer
    opponent = game_data['black'] if username == game_data['white'] else game_data['white']

    # Find ALL opponent session IDs
    opponent_sids = []
    for sid, user_data in online_users.items():
        if user_data == opponent:
            opponent_sids.append(sid)

    for opponent_sid in opponent_sids:
        socketio.emit('rematch_offered', {'from_player': username}, to=opponent_sid)

@socketio.on('accept_rematch')
def on_accept_rematch(data):
    username = session.get('username')
    room_id = data['room_id']
    game_data = games.get(room_id)

    if not game_data or username not in [game_data['white'], game_data['black']]:
        return

    # Check if rematch already in progress
    if game_data.get('rematch_in_progress'):
        return

    # Add this player to rematch requests
    if 'rematch_requests' not in game_data:
        game_data['rematch_requests'] = set()

    game_data['rematch_requests'].add(username)

    # Check if both players want rematch
    if len(game_data['rematch_requests']) >= 2 and not game_data.get('rematch_in_progress'):
        # Mark rematch as in progress to prevent duplicates
        game_data['rematch_in_progress'] = True

        # Both players agreed - start new game
        # Swap colors for rematch
        old_white = game_data['white']
        old_black = game_data['black']

        new_white = old_black
        new_black = old_white

        # Parse time control
        time_control = game_data.get('time_control', '3+2')
        parts = time_control.split('+')
        minutes = int(parts[0])
        increment = int(parts[1]) if len(parts) > 1 else 0
        base_time = minutes * 60

        # Create new game
        game = NineMensMorris()
        new_game_id = str(uuid.uuid4())
        games[new_game_id] = {
            'id': new_game_id,
            'white': new_white,
            'black': new_black,
            'game': game,
            'moves': [],
            'positions': [game.board[:]],
            'time_control': time_control,
            'start_time': datetime.now().isoformat(),
            'status': 'waiting_first_move',
            'berserk': {'white': False, 'black': False},
            'timers': {'white': base_time, 'black': base_time},
            'increment': increment,
            'last_move_time': datetime.now(),
            'server_start_time': time.time(),
            'active_timer': 'white',
            'timer_started': False,
            'first_move_start_time': time.time(),
            'first_move_deadline': time.time() + 20,
            'waiting_for_first_move': 'white',
            'white_first_move_made': False,
            'black_first_move_made': False
        }

        # Start server-authoritative timer for rematch
        start_game_timer(new_game_id)

        # Get ALL session IDs for both players
        white_sids = []
        black_sids = []

        for sid, user_data in online_users.items():
            if user_data == new_white:
                white_sids.append(sid)
            elif user_data == new_black:
                black_sids.append(sid)

        # Add all sessions to the new game room
        for white_sid in white_sids:
            join_room(new_game_id, sid=white_sid)
        for black_sid in black_sids:
            join_room(new_game_id, sid=black_sid)

        # Get user data for both players
        white_user_data = users.get(new_white, {})
        black_user_data = users.get(new_black, {})
        
        # Add ranking colors to user data
        white_user_data['ranking_color'] = get_ranking_color(new_white)
        black_user_data['ranking_color'] = get_ranking_color(new_black)

        # Calculate initial piece counts
        piece_counts = calculate_piece_counts(games[new_game_id])

        # Get ranking badges for both players
        white_badges = get_ranking_badge(new_white)
        black_badges = get_ranking_badge(new_black)
        
        # Send rematch accepted event to ALL sessions of both players
        rematch_data = {
            'room_id': new_game_id,
            'white': new_white,
            'black': new_black,
            'time_control': time_control,
            'white_user_data': white_user_data,
            'black_user_data': black_user_data,
            'piece_counts': piece_counts,
            'white_badges': white_badges,
            'black_badges': black_badges
        }

        for white_sid in white_sids:
            socketio.emit('rematch_accepted', {
                **rematch_data,
                'your_color': 'white'
            }, to=white_sid)

        for black_sid in black_sids:
            socketio.emit('rematch_accepted', {
                **rematch_data,
                'your_color': 'black'
            }, to=black_sid)

        # Send first move countdown start signal for rematch game
        socketio.emit('first_move_countdown_start', {
            'seconds_left': 20,
            'server_start_time': time.time(),
            'waiting_for': 'white'
        }, room=new_game_id)

        print(f"Rematch created: {new_white} vs {new_black} in room {new_game_id}")
    else:
        # Only one player wants rematch, notify opponent
        opponent = game_data['black'] if username == game_data['white'] else game_data['white']

        # Find ALL opponent session IDs
        opponent_sids = []
        for sid, user_data in online_users.items():
            if user_data == opponent:
                opponent_sids.append(sid)

        for opponent_sid in opponent_sids:
            socketio.emit('rematch_offered', {'from_player': username}, to=opponent_sid)

# Helper function to calculate piece counts
def calculate_piece_counts(game_data):
    """Calculate piece counts for both players"""
    game = game_data['game']
    board = game.board

    # Count pieces on board
    white_on_board = sum(1 for piece in board if piece == 'white')
    black_on_board = sum(1 for piece in board if piece == 'black')

    # Calculate placed pieces based on game phase and pieces remaining
    if game.phase == 1:  # Placement phase
        white_placed = 9 - game.white_pieces  # pieces placed = total - remaining
        black_placed = 9 - game.black_pieces
    else:  # Moving/flying phase - all pieces have been placed
        white_placed = 9
        black_placed = 9

    return {
        'white': {
            'placed': white_placed,
            'on_board': white_on_board
        },
        'black': {
            'placed': black_placed,
            'on_board': black_on_board
        }
    }

@socketio.on('tournament_back_to_lobby')
def on_tournament_back_to_lobby(data):
    """Handle player returning to tournament lobby - requeue after 5 seconds"""
    username = session.get('username')
    tournament_id = data.get('tournament_id')
    room_id = data.get('room_id')
    
    if not username or not tournament_id:
        return
    
    tournament = tournaments.get(tournament_id)
    if not tournament or tournament.get('status') != 'active':
        return
    
    # Remove player from game menu and paused users
    players_in_game_menu.discard(username)
    game_menu_timestamps.pop(username, None)
    paused_users.discard(username)
    
    # Schedule requeue after 5 seconds
    def delayed_requeue():
        time.sleep(5)  # Wait 5 seconds before re-pairing
        if username not in paused_users and username not in players_in_game_menu and not is_player_in_game(username):
            if username in tournament.get('players', {}):
                match_tournament_players(tournament_id, username)
    
    import threading
    threading.Thread(target=delayed_requeue, daemon=True).start()
    
    print(f"{username} returned to tournament {tournament_id[:8]}... - will be requeued in 5 seconds")

@socketio.on('tournament_pause_for_analysis')
def on_tournament_pause_for_analysis(data):
    """Handle player pausing in tournament to analyze their game"""
    username = session.get('username')
    tournament_id = data.get('tournament_id')
    room_id = data.get('room_id')
    
    if not username or not tournament_id:
        return
    
    tournament = tournaments.get(tournament_id)
    if not tournament:
        return
    
    # Remove from game menu (they clicked a button)
    players_in_game_menu.discard(username)
    game_menu_timestamps.pop(username, None)
    
    # Add player to paused users so they won't be paired
    paused_users.add(username)
    
    print(f"{username} paused in tournament {tournament_id[:8]}... for game analysis")

@socketio.on('tournament_resume')
def on_tournament_resume(data):
    """Handle player resuming from analysis mode in tournament"""
    username = session.get('username')
    tournament_id = data.get('tournament_id')
    
    if not username or not tournament_id:
        return
    
    tournament = tournaments.get(tournament_id)
    if not tournament or tournament.get('status') != 'active':
        return
    
    # Remove player from paused users and game menu
    paused_users.discard(username)
    players_in_game_menu.discard(username)
    game_menu_timestamps.pop(username, None)
    
    # Schedule requeue after 5 seconds (same as back to lobby)
    def delayed_requeue():
        time.sleep(5)  # Wait 5 seconds before re-pairing
        if username not in paused_users and username not in players_in_game_menu and not is_player_in_game(username):
            if username in tournament.get('players', {}):
                match_tournament_players(tournament_id, username)
    
    import threading
    threading.Thread(target=delayed_requeue, daemon=True).start()
    
    print(f"{username} resumed tournament {tournament_id[:8]}... - will be requeued in 5 seconds")

def initialize_app_background():
    try:
        with app.app_context():
            db.create_all()
            load_all_data()
            
            initialize_highest_titles()
            
            create_scheduled_tournaments()
            start_scheduled_tournaments()
            print(f"Initialized {len(tournaments)} tournaments at startup")
            active_count = len([t for t in tournaments.values() if t.get('status') == 'active'])
            print(f"Active tournaments: {active_count}")
            
            init_admin_tournament_counter()
            print(f"Admin tournament counter initialized to: {admin_tournament_counter}")
            
            pairing_thread = threading.Thread(target=run_continuous_pairing, daemon=True)
            pairing_thread.start()
            print("Started continuous tournament pairing thread")
    except Exception as e:
        print(f"Error during initialization: {e}")

def start_initialization():
    threading.Thread(target=initialize_app_background, daemon=True).start()

import sys
print(f"[startup] Python {sys.version}", flush=True)
print(f"[startup] Starting initialization thread...", flush=True)
start_initialization()
print(f"[startup] Module loaded, ready to serve requests", flush=True)

if __name__ == '__main__':
    print(f"[startup] Starting socketio.run on 0.0.0.0:5000", flush=True)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
