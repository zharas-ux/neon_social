import os
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, flash, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = "neon-social-secret-key-change-me"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "neon_social.db")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB на файл

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Пожалуйста, войдите, чтобы продолжить."


# ---------------------- МОДЕЛИ ----------------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    avatar = db.Column(db.String(255), default="default.png")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User", backref="posts")


class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("post_id", "user_id", name="unique_like"),)

    post = db.relationship("Post", backref="likes")


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User")
    post = db.relationship("Post", backref="comments")


class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending / accepted / declined
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    text = db.Column(db.String(300), nullable=False)
    link = db.Column(db.String(300), default="")
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------- ХЕЛПЕРЫ ----------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def get_friend_ids(user_id):
    accepted = FriendRequest.query.filter(
        FriendRequest.status == "accepted",
        db.or_(FriendRequest.sender_id == user_id, FriendRequest.receiver_id == user_id)
    ).all()
    ids = set()
    for fr in accepted:
        ids.add(fr.receiver_id if fr.sender_id == user_id else fr.sender_id)
    return ids


def are_friends(a_id, b_id):
    return b_id in get_friend_ids(a_id)


def friend_request_between(a_id, b_id):
    return FriendRequest.query.filter(
        db.or_(
            db.and_(FriendRequest.sender_id == a_id, FriendRequest.receiver_id == b_id),
            db.and_(FriendRequest.sender_id == b_id, FriendRequest.receiver_id == a_id),
        )
    ).order_by(FriendRequest.created_at.desc()).first()


def add_notification(user_id, text, link=""):
    n = Notification(user_id=user_id, text=text, link=link)
    db.session.add(n)


@app.context_processor
def inject_globals():
    if current_user.is_authenticated:
        unread_notif = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        unread_msgs = Message.query.filter_by(receiver_id=current_user.id, is_read=False).count()
        pending_requests = FriendRequest.query.filter_by(receiver_id=current_user.id, status="pending").count()
    else:
        unread_notif = unread_msgs = pending_requests = 0
    return dict(unread_notif=unread_notif, unread_msgs=unread_msgs, pending_requests=pending_requests)


# ---------------------- АУТЕНТИФИКАЦИЯ ----------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not password:
            flash("Заполните все поля.", "error")
        elif password != confirm:
            flash("Пароли не совпадают.", "error")
        elif User.query.filter_by(username=username).first():
            flash("Такой логин уже занят.", "error")
        else:
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Регистрация прошла успешно! Теперь войдите.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("feed"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            # уведомляем друзей о входе
            for fid in get_friend_ids(user.id):
                add_notification(fid, f"{user.username} только что вошёл(а) в сеть.")
            db.session.commit()
            flash(f"Добро пожаловать, {user.username}!", "success")
            return redirect(url_for("feed"))
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------- ЛЕНТА / ПОСТЫ ----------------------

@app.route("/", methods=["GET", "POST"])
@login_required
def feed():
    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            post = Post(user_id=current_user.id, content=content)
            db.session.add(post)
            db.session.commit()
        return redirect(url_for("feed"))

    visible_ids = get_friend_ids(current_user.id) | {current_user.id}
    posts = Post.query.filter(Post.user_id.in_(visible_ids)).order_by(Post.created_at.desc()).all()
    liked_post_ids = {l.post_id for l in Like.query.filter_by(user_id=current_user.id).all()}
    return render_template("feed.html", posts=posts, liked_post_ids=liked_post_ids)


@app.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    post = Post.query.get_or_404(post_id)
    existing = Like.query.filter_by(post_id=post.id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(Like(post_id=post.id, user_id=current_user.id))
        if post.user_id != current_user.id:
            add_notification(post.user_id, f"{current_user.username} лайкнул(а) ваш пост.", url_for("feed"))
    db.session.commit()
    return redirect(request.referrer or url_for("feed"))


@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def comment_post(post_id):
    post = Post.query.get_or_404(post_id)
    text = request.form.get("content", "").strip()
    if text:
        db.session.add(Comment(post_id=post.id, user_id=current_user.id, content=text))
        if post.user_id != current_user.id:
            add_notification(post.user_id, f"{current_user.username} прокомментировал(а) ваш пост.", url_for("feed"))
        db.session.commit()
    return redirect(request.referrer or url_for("feed"))


# ---------------------- ПРОФИЛЬ И АВАТАР ----------------------

@app.route("/profile/<username>")
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.created_at.desc()).all()
    friendship = None
    if user.id != current_user.id:
        friendship = friend_request_between(current_user.id, user.id)
    return render_template("profile.html", user=user, posts=posts, friendship=friendship)


@app.route("/upload_avatar", methods=["POST"])
@login_required
def upload_avatar():
    file = request.files.get("avatar")
    if file and file.filename and allowed_file(file.filename):
        ext = file.filename.rsplit(".", 1)[1].lower()
        filename = secure_filename(f"user_{current_user.id}.{ext}")
        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        current_user.avatar = filename
        db.session.commit()
        flash("Аватар обновлён!", "success")
    else:
        flash("Недопустимый файл. Разрешены: png, jpg, jpeg, gif, webp.", "error")
    return redirect(url_for("profile", username=current_user.username))


# ---------------------- ДРУЗЬЯ / ЗАПРОСЫ ----------------------

@app.route("/friend_request/send/<int:user_id>", methods=["POST"])
@login_required
def send_friend_request(user_id):
    if user_id == current_user.id:
        return redirect(url_for("feed"))
    target = User.query.get_or_404(user_id)
    existing = friend_request_between(current_user.id, target.id)
    if existing and existing.status in ("pending", "accepted"):
        flash("Запрос уже отправлен или вы уже друзья.", "error")
    else:
        fr = FriendRequest(sender_id=current_user.id, receiver_id=target.id, status="pending")
        db.session.add(fr)
        add_notification(target.id, f"{current_user.username} отправил(а) вам запрос в друзья.", url_for("friend_requests"))
        db.session.commit()
        flash("Запрос отправлен!", "success")
    return redirect(url_for("profile", username=target.username))


@app.route("/friend_requests")
@login_required
def friend_requests():
    incoming = FriendRequest.query.filter_by(receiver_id=current_user.id, status="pending").all()
    outgoing = FriendRequest.query.filter_by(sender_id=current_user.id, status="pending").all()
    return render_template("friend_requests.html", incoming=incoming, outgoing=outgoing)


@app.route("/friend_request/<int:req_id>/accept", methods=["POST"])
@login_required
def accept_friend_request(req_id):
    fr = FriendRequest.query.get_or_404(req_id)
    if fr.receiver_id != current_user.id:
        abort(403)
    fr.status = "accepted"
    add_notification(fr.sender_id, f"{current_user.username} принял(а) ваш запрос в друзья.", url_for("profile", username=current_user.username))
    db.session.commit()
    flash("Запрос принят, теперь вы друзья!", "success")
    return redirect(url_for("friend_requests"))


@app.route("/friend_request/<int:req_id>/decline", methods=["POST"])
@login_required
def decline_friend_request(req_id):
    fr = FriendRequest.query.get_or_404(req_id)
    if fr.receiver_id != current_user.id:
        abort(403)
    fr.status = "declined"
    db.session.commit()
    flash("Запрос отклонён.", "success")
    return redirect(url_for("friend_requests"))


# ---------------------- СООБЩЕНИЯ ----------------------

@app.route("/messages")
@login_required
def messages():
    friend_ids = get_friend_ids(current_user.id)
    friends = User.query.filter(User.id.in_(friend_ids)).all() if friend_ids else []
    return render_template("messages.html", friends=friends)


@app.route("/messages/<int:user_id>", methods=["GET", "POST"])
@login_required
def chat(user_id):
    other = User.query.get_or_404(user_id)
    if not are_friends(current_user.id, other.id):
        flash("Личные сообщения доступны только друзьям.", "error")
        return redirect(url_for("messages"))

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            db.session.add(Message(sender_id=current_user.id, receiver_id=other.id, content=content))
            add_notification(other.id, f"Новое сообщение от {current_user.username}.", url_for("chat", user_id=current_user.id))
            db.session.commit()
        return redirect(url_for("chat", user_id=other.id))

    Message.query.filter_by(sender_id=other.id, receiver_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()

    chat_messages = Message.query.filter(
        db.or_(
            db.and_(Message.sender_id == current_user.id, Message.receiver_id == other.id),
            db.and_(Message.sender_id == other.id, Message.receiver_id == current_user.id),
        )
    ).order_by(Message.created_at.asc()).all()
    return render_template("chat.html", other=other, chat_messages=chat_messages)


# ---------------------- УВЕДОМЛЕНИЯ ----------------------

@app.route("/notifications")
@login_required
def notifications():
    items = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return render_template("notifications.html", items=items)


# ---------------------- ЗАПУСК ----------------------

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
