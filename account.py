# account.py
from flask import Blueprint, redirect, url_for, flash
from flask_login import login_required, current_user
from datetime import datetime

account_bp = Blueprint("account", __name__)

@account_bp.route("/become-contractor", methods=["POST"])
@login_required
def become_contractor():
    # Lazy imports to avoid circulars
    from app import db, ContractorProfile, session

    if getattr(current_user, "is_contractor", False):
        flash("You're already a contractor.", "info")
        return redirect(url_for("dashboard"))

    # Upgrade flags
    current_user.is_contractor = True
    # Keep customer capability too
    if not getattr(current_user, "is_customer", False):
        current_user.is_customer = True

    # Create a contractor profile if missing
    prof = ContractorProfile.query.filter_by(user_id=current_user.id).first()
    if not prof:
        prof = ContractorProfile(
            user_id=current_user.id,
            display_name=current_user.display_name or None,
            created_at=datetime.utcnow(),
        )
        db.session.add(prof)

    db.session.commit()

    # Switch active role in the session so nav/context updates immediately
    session["active_role"] = "contractor"
    flash("Welcome! Your account can now bid as a contractor.", "success")
    return redirect(url_for("edit_profile"))
