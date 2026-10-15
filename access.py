"""명령어 접근 규칙."""

ADMIN_ONLY_GUILD_IDS = frozenset({1555854735251087380})


def is_admin(interaction) -> bool:
    """서버 관리자(Administrator 권한)나 서버 주인인지. 서버 밖(DM)이거나 알 수 없으면 False."""
    if getattr(interaction, "guild_id", None) is None:
        return False
    # 디스코드가 요청과 함께 알려준 현재 권한을 쓴다(서버 주인은 모든 권한을 가진다).
    permissions = getattr(interaction, "permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    owner_id = getattr(getattr(interaction, "guild", None), "owner_id", None)
    return owner_id is not None and getattr(getattr(interaction, "user", None), "id", None) == owner_id


def allowed(interaction, admin_only=ADMIN_ONLY_GUILD_IDS) -> bool:
    guild_id = interaction.guild_id
    if guild_id is None or guild_id not in admin_only:
        return True
    return is_admin(interaction)
