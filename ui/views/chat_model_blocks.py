"""Presentation groups only: individual turns and their stored ids stay intact."""
from shiboken6 import isValid

MODEL_PART_GAP = 6  # px between thoughts and the answer in one visual block.


def owner(turn):
    value = getattr(turn, '_display_owner', turn)
    return value if isValid(value) else turn


def members(turn):
    return tuple(item for item in getattr(turn, '_display_members', (turn,)) if isValid(item))


def expand(turns):
    return {item for turn in turns for item in members(turn)}


def inherit_resume_identity(view, turn):
    target = getattr(view, '_resume_target', None)
    if target is not None and hasattr(target, '_display_group_id'):
        turn._display_group_id = target._display_group_id


def refresh(rows):
    from ui.views.chat_view import UserMessageTurn, ThoughtMessageTurn, AssistantMessageTurn
    groups = []
    current, identity = [], None
    for turn in rows:
        key = getattr(turn, '_display_group_id', None)
        if key is None:
            key = (getattr(turn, '_run_state', None) or {}).get('run_id')
        if isinstance(turn, UserMessageTurn):
            if current:
                groups.append(current)
            groups.append([turn])
            current, identity = [], None
            continue
        # Saved messages carry explicit group identities; adjacent independent
        # AI messages must never acquire each other's thoughts. Unlabelled UI
        # turns join only a thought followed by its answer.
        joins = current and ((key is not None and key == identity) or
                (key is None and identity is None and isinstance(current[-1], ThoughtMessageTurn)))
        if current and not joins:
            groups.append(current)
            current = []
        current.append(turn)
        identity = key
    if current:
        groups.append(current)
    for group in groups:
        representative = next((turn for turn in reversed(group) if isinstance(turn, AssistantMessageTurn)),
                              next((turn for turn in reversed(group) if getattr(turn,'is_subagent_block',False)),group[0]))
        for index, turn in enumerate(group):
            turn._display_owner = representative
            turn._display_members = tuple(group)
            turn._display_part = 'single' if len(group) == 1 else 'first' if index == 0 else 'last' if index == len(group)-1 else 'middle'
            layout = turn.layout()
            if not hasattr(turn, '_display_margins'):
                margins = layout.contentsMargins()
                turn._display_margins = (margins.left(), margins.top(), margins.right(), margins.bottom())
            left, top, right, bottom = turn._display_margins
            wanted = (left, top if index == 0 else MODEL_PART_GAP, right, bottom + (8 if index == len(group)-1 else 0))
            actual = layout.contentsMargins()
            if wanted != (actual.left(), actual.top(), actual.right(), actual.bottom()):
                layout.setContentsMargins(*wanted)
            bar = getattr(turn, 'actions_bar', None)
            if bar is not None:
                bar.setVisible(turn is representative)
    return [owner(group[0]) for group in groups]


def outline_rule(turn):
    part = getattr(turn, '_display_part', 'single')
    if part == 'single':
        return ''
    first, last = part == 'first', part == 'last'
    return (f'border-top-left-radius:{8 if first else 0}px; border-top-right-radius:{8 if first else 0}px;'
            f'border-bottom-left-radius:{8 if last else 0}px; border-bottom-right-radius:{8 if last else 0}px;')
