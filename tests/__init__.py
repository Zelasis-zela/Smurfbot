import json
import os

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Réponses réelles de l'API HenrikDev (partie "data" seulement), anonymisées :
# le joueur analysé est TestSmurf#0001, les autres Joueur{n}#T{n}, puuid / id de partie remplacés par des uuid5.
PLAYER_PUUID = "ca8db163-28ec-5c25-88b7-51d4839245e3"
MATCH_FIXTURES = ["match_v4_list0", "match_v4_list1", "match_v4_list2", "match_v4_single", "match_v4_single2"]


def load_fixture(name):
    # load_fixture("stored_matches") -> contenu de tests/fixtures/stored_matches.json
    if not name.endswith(".json"):
        name += ".json"
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as f:
        return json.load(f)
