"""File to sum up the experience from drops."""

import json
import asyncio

class TestClass:
    """Test class"""

    def __init__(self):
        self.name = "TestClass"

    def init_file_exp(self):
        """Reset the file with experience."""

        modifiers ={
        "1234212213": 0
        }

        with open('expSummary.json', 'w', encoding="utf-8") as file:
            json.dump(modifiers, file)

    def add_file_exp(self, player_id, experience):
        """Check if an ID exists, then add exp."""

        with open('expSummary.json', 'r', encoding="utf-8") as file:
            players = json.load(file)

        player_id = str(player_id)
        print(type(players))
        file.close()

        if player_id in players:
            players[player_id] += experience
        else:
            players[player_id] = experience
        print(players)

        with open('expSummary.json', 'w', encoding="utf-8") as file:
            json.dump(players, file)

    def show_fileexp(self):
        """Show the table with an exp summary."""

        with open('expSummary.json', 'r', encoding="utf-8") as file:
            players = json.load(file)

        text = []
        for key, value in players.items():
            text.append(f"<@{key}>: {value}")

        exp_table = '\n'.join(text)
        print(exp_table)

print("Test executed.")
Test = TestClass()

Test.init_file_exp()
Test.add_file_exp(12345, 500)
Test.add_file_exp(12345, 500)
Test.add_file_exp(123456, 1250)
Test.add_file_exp(123456, 1250)
Test.add_file_exp(21321223, 1250)
Test.show_fileexp()
