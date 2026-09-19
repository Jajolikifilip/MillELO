from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import JSON
from datetime import datetime


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


class Game(db.Model):
    __tablename__ = 'games'
    
    id = db.Column(db.String(50), primary_key=True)
    white = db.Column(db.String(50), nullable=False, index=True)
    black = db.Column(db.String(50), nullable=False, index=True)
    winner = db.Column(db.String(50))  # 'white', 'black', 'draw', or username
    status = db.Column(db.String(20), default='finished')
    end_reason = db.Column(db.String(50))
    time_control = db.Column(db.String(20))
    rating_type = db.Column(db.String(20))
    start_time = db.Column(db.String(50))
    end_time = db.Column(db.String(50))
    moves = db.Column(JSON, default=list)
    rating_changes = db.Column(JSON, default=dict)
    tournament_id = db.Column(db.String(50))
    white_berserk = db.Column(db.Boolean, default=False)
    black_berserk = db.Column(db.Boolean, default=False)
    positions = db.Column(JSON, default=list)
    
    def to_dict(self):
        return {
            'id': self.id,
            'white': self.white,
            'black': self.black,
            'winner': self.winner,
            'status': self.status,
            'end_reason': self.end_reason,
            'time_control': self.time_control,
            'rating_type': self.rating_type,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'moves': self.moves or [],
            'rating_changes': self.rating_changes or {},
            'tournament_id': self.tournament_id,
            'white_berserk': self.white_berserk,
            'black_berserk': self.black_berserk,
            'positions': self.positions or []
        }


class Friendship(db.Model):
    __tablename__ = 'friendships'
    
    id = db.Column(db.Integer, primary_key=True)
    user1 = db.Column(db.String(50), nullable=False, index=True)
    user2 = db.Column(db.String(50), nullable=False, index=True)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'accepted'
    created = db.Column(db.String(50), default=lambda: datetime.now().isoformat())
    
    def to_dict(self):
        return {
            'id': self.id,
            'user1': self.user1,
            'user2': self.user2,
            'status': self.status,
            'created': self.created
        }


class PrivateMessage(db.Model):
    __tablename__ = 'private_messages'
    
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(50), nullable=False, index=True)
    receiver = db.Column(db.String(50), nullable=False, index=True)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.String(50), default=lambda: datetime.now().isoformat())
    read = db.Column(db.Boolean, default=False)
    
    def to_dict(self):
        return {
            'id': self.id,
            'sender': self.sender,
            'receiver': self.receiver,
            'message': self.message,
            'timestamp': self.timestamp,
            'read': self.read
        }


class BanRecord(db.Model):
    __tablename__ = 'ban_records'
    
    id = db.Column(db.Integer, primary_key=True)
    banned_user = db.Column(db.String(50), nullable=False, index=True)
    banned_by = db.Column(db.String(50), nullable=False)
    reason = db.Column(db.Text, default='No reason given')
    timestamp = db.Column(db.String(50), default=lambda: datetime.now().isoformat())
    is_active = db.Column(db.Boolean, default=True)
    unbanned_by = db.Column(db.String(50), default=None)
    unbanned_at = db.Column(db.String(50), default=None)
    
    def to_dict(self):
        return {
            'id': self.id,
            'banned_user': self.banned_user,
            'banned_by': self.banned_by,
            'reason': self.reason,
            'timestamp': self.timestamp,
            'is_active': self.is_active,
            'unbanned_by': self.unbanned_by,
            'unbanned_at': self.unbanned_at
        }


class ArchivedTournament(db.Model):
    __tablename__ = 'archived_tournaments'
    
    id = db.Column(db.String(100), primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    tournament_type = db.Column(db.String(50))
    time_control = db.Column(db.String(20))
    start_time = db.Column(db.String(50))
    end_time = db.Column(db.String(50))
    finished_time = db.Column(db.String(50))
    status = db.Column(db.String(20), default='finished')
    players = db.Column(JSON, default=dict)
    color = db.Column(db.String(20), default='#FFD700')
    final_leaderboard = db.Column(JSON, default=list)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'tournament_type': self.tournament_type,
            'time_control': self.time_control,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'finished_time': self.finished_time,
            'status': self.status,
            'players': self.players or {},
            'color': self.color,
            'final_leaderboard': self.final_leaderboard or []
        }


class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password = db.Column(db.String(256), nullable=False)
    bullet_rating = db.Column(db.Integer, default=100)
    blitz_rating = db.Column(db.Integer, default=100)
    games_played = db.Column(JSON, default=lambda: {'bullet': 0, 'blitz': 0})
    wins = db.Column(JSON, default=lambda: {'bullet': 0, 'blitz': 0})
    losses = db.Column(JSON, default=lambda: {'bullet': 0, 'blitz': 0})
    draws = db.Column(JSON, default=lambda: {'bullet': 0, 'blitz': 0})
    created = db.Column(db.String(50), default=lambda: datetime.now().isoformat())
    color = db.Column(db.String(20), default='#c9c9c9')
    is_admin = db.Column(db.Boolean, default=False)
    admin_rank = db.Column(db.String(20), default=None)  # None, 'admin', 'dragon', 'galaxy', 'creator'
    best_wins = db.Column(JSON, default=lambda: {'bullet': [], 'blitz': []})
    tournaments_won = db.Column(JSON, default=lambda: {'daily': 0, 'weekly': 0, 'monthly': 0, 'marathon': 0, 'world_cup': 0})
    trophies = db.Column(JSON, default=list)
    elo_history = db.Column(JSON, default=lambda: {'bullet': [], 'blitz': []})
    highest_title = db.Column(db.String(20), default=None, nullable=True)
    highest_title_color = db.Column(db.String(20), default='#888888')
    likes = db.Column(JSON, default=lambda: {'count': 0, 'liked_by': []})
    piece_design = db.Column(db.String(50), default='classic')
    
    def to_dict(self):
        return {
            'username': self.username,
            'password': self.password,
            'bullet_rating': self.bullet_rating,
            'blitz_rating': self.blitz_rating,
            'games_played': self.games_played or {'bullet': 0, 'blitz': 0},
            'wins': self.wins or {'bullet': 0, 'blitz': 0},
            'losses': self.losses or {'bullet': 0, 'blitz': 0},
            'draws': self.draws or {'bullet': 0, 'blitz': 0},
            'created': self.created,
            'color': self.color,
            'is_admin': self.is_admin,
            'admin_rank': self.admin_rank,
            'best_wins': self.best_wins or {'bullet': [], 'blitz': []},
            'tournaments_won': self.tournaments_won or {'daily': 0, 'weekly': 0, 'monthly': 0, 'marathon': 0, 'world_cup': 0},
            'trophies': self.trophies or [],
            'elo_history': self.elo_history or {'bullet': [], 'blitz': []},
            'highest_title': self.highest_title,
            'highest_title_color': self.highest_title_color,
            'likes': self.likes or {'count': 0, 'liked_by': []},
            'piece_design': self.piece_design or 'classic'
        }
    
    def update_from_dict(self, data):
        self.password = data.get('password', self.password)
        self.bullet_rating = data.get('bullet_rating', self.bullet_rating)
        self.blitz_rating = data.get('blitz_rating', self.blitz_rating)
        self.games_played = data.get('games_played', self.games_played)
        self.wins = data.get('wins', self.wins)
        self.losses = data.get('losses', self.losses)
        self.draws = data.get('draws', self.draws)
        self.color = data.get('color', self.color)
        self.is_admin = data.get('is_admin', self.is_admin)
        self.admin_rank = data.get('admin_rank', self.admin_rank)
        self.best_wins = data.get('best_wins', self.best_wins)
        self.tournaments_won = data.get('tournaments_won', self.tournaments_won)
        self.trophies = data.get('trophies', self.trophies)
        self.elo_history = data.get('elo_history', self.elo_history)
        self.highest_title = data.get('highest_title', self.highest_title)
        self.highest_title_color = data.get('highest_title_color', self.highest_title_color)
        self.likes = data.get('likes', self.likes)
        self.piece_design = data.get('piece_design', self.piece_design)
