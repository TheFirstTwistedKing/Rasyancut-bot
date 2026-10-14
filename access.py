"""명령어 접근 규칙."""

ADMIN_ONLY_GUILD_IDS = frozenset({1555854735251087380})


def allowed(interaction, admin_only=ADMIN_ONLY_GUILD_IDS) -> bool:
    guild_id = interaction.guild_id
    if guild_id is None or guild_id not in admin_only:
        return True
    # 디스코드가 요청과 함께 알려준 현재 권한을 쓴다(서버 주인은 모든 권한을 가진다).
    return bool(interaction.permissions.administrator) or \
        interaction.user.id == getattr(interaction.guild, "owner_id", None)
