"""Static structural data for the 2026 World Cup bracket.

Everything here is the *deterministic* tournament structure (verified against the
official FIFA / Wikipedia bracket), as opposed to the team/odds data which lives
in data/teams.csv.  Groups are A..L; positions within a group are 1 (winner),
2 (runner-up), 3 (third place).
"""

GROUP_LETTERS = list("ABCDEFGHIJKL")  # 12 groups

# ---------------------------------------------------------------------------
# Round of 32 matches (match number -> the two participants).
# A participant is one of:
#   ("W", "E")      winner of group E
#   ("R", "C")      runner-up of group C
#   ("3", slot_id)  a third-place qualifier assigned to this slot (see THIRD_SLOTS)
# Source: 2026 FIFA World Cup knockout stage bracket.
# ---------------------------------------------------------------------------
R32_MATCHES = {
    73: (("R", "A"), ("R", "B")),
    74: (("W", "E"), ("3", 74)),
    75: (("W", "F"), ("R", "C")),
    76: (("W", "C"), ("R", "F")),
    77: (("W", "I"), ("3", 77)),
    78: (("R", "E"), ("R", "I")),
    79: (("W", "A"), ("3", 79)),
    80: (("W", "L"), ("3", 80)),
    81: (("W", "D"), ("3", 81)),
    82: (("W", "G"), ("3", 82)),
    83: (("R", "K"), ("R", "L")),
    84: (("W", "H"), ("R", "J")),
    85: (("W", "B"), ("3", 85)),
    86: (("W", "J"), ("R", "H")),
    87: (("W", "K"), ("3", 87)),
    88: (("R", "D"), ("R", "G")),
}

# Each third-place slot can only be filled by a third-place team from one of
# these groups (FIFA's allowable-group sets for the 8 reserved slots).
THIRD_SLOTS = {
    74: set("ABCDF"),
    77: set("CDFGH"),
    79: set("CEFHI"),
    80: set("EHIJK"),
    81: set("BEFIJ"),
    82: set("AEHIJ"),
    85: set("EFGIJ"),
    87: set("DEIJL"),
}

# Linear bracket order of the 16 R32 matches such that folding adjacent winners
# pairwise reproduces matches 89..104.  Derived from the R16/QF/SF/Final tree:
#   R16: 89=(74,77) 90=(73,75) 91=(76,78) 92=(79,80)
#        93=(83,84) 94=(81,82) 95=(86,88) 96=(85,87)
#   QF:  97=(89,90) 98=(93,94) 99=(91,92) 100=(95,96)
#   SF:  101=(97,98) 102=(99,100)   Final: 104=(101,102)
BRACKET_ORDER = [74, 77, 73, 75, 83, 84, 81, 82, 76, 78, 79, 80, 86, 88, 85, 87]

# Round names and the pool points awarded for *winning* a match in that round.
ROUND_POINTS = {
    "R32": 5,
    "R16": 7,
    "QF": 10,
    "SF": 15,
    "FINAL": 20,
}
ROUND_ORDER = ["R32", "R16", "QF", "SF", "FINAL"]

# Pool scoring for the group stage (per match, applied to each of your teams).
GROUP_WIN_PTS = 3
GROUP_DRAW_PTS = 1
