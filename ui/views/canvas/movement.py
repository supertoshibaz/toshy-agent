"""Точное применение общего смещения без повторной привязки каждой карточки."""
def place_cards(scene, positions):
    previous = getattr(scene, '_exact_card_move', False)
    scene._exact_card_move = True
    try:
        for card, position in positions:
            card.setPos(position)
    finally:
        scene._exact_card_move = previous
