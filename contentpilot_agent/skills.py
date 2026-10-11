"""Versioned business skills, independent of Codex's own skills."""

SKILLS = {
    "scene": {
        "id": "scene",
        "version": "1",
        "name": "场景种草",
        "instructions": "围绕目标用户的真实日常场景，按痛点、商品事实、使用建议组织内容。不得虚构亲测体验。",
    },
    "decision": {
        "id": "decision",
        "version": "1",
        "name": "购买决策",
        "instructions": "说明适用人群、已知规格和选择依据，主动指出未提供的信息。不得虚构竞品对比或优惠。",
    },
    "tutorial": {
        "id": "tutorial",
        "version": "1",
        "name": "使用指南",
        "instructions": "以步骤清晰的使用指南组织内容；不确定的使用方法应提醒查阅说明书，不作安全或效果保证。",
    },
}


def skill_snapshot(skill_id):
    return dict(SKILLS[skill_id])
