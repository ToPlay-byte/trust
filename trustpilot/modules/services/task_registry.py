from scenarios.day_1 import Day1Scenario
from scenarios.day_2 import Day2Scenario
from scenarios.day_3 import Day3Scenario
from scenarios.day_4 import Day4Scenario
from scenarios.day_5 import Day5Scenario
from scenarios.day_6 import Day6Scenario
from scenarios.day_7 import Day7Scenario
from scenarios.day_8 import Day8Scenario
from scenarios.day_9 import Day9Scenario
from scenarios.day_10 import Day10Scenario
from scenarios.day_11 import Day11Scenario
from scenarios.day_12 import Day12Scenario
from scenarios.day_13 import Day13Scenario
from scenarios.day_14 import Day14Scenario
from scenarios.day_15 import Day15Scenario
from scenarios.day_16 import Day16Scenario
from scenarios.day_17 import Day17Scenario
from scenarios.day_18 import Day18Scenario
from scenarios.day_19 import Day19Scenario
from scenarios.day_20 import Day20Scenario
from scenarios.day_21 import Day21Scenario

SCENARIO_REGISTRY = {
    "day_1": Day1Scenario,
    "day_2": Day2Scenario,
    "day_3": Day3Scenario,
    "day_4": Day4Scenario,
    "day_5": Day5Scenario,
    "day_6": Day6Scenario,
    "day_7": Day7Scenario,
    "day_8": Day8Scenario,
    "day_9": Day9Scenario,
    "day_10": Day10Scenario,
    "day_11": Day11Scenario,
    "day_12": Day12Scenario,
    "day_13": Day13Scenario,
    "day_14": Day14Scenario,
    "day_15": Day15Scenario,
    "day_16": Day16Scenario,
    "day_17": Day17Scenario,
    "day_18": Day18Scenario,
    "day_19": Day19Scenario,
    "day_20": Day20Scenario,
    "day_21": Day21Scenario,
}


class TaskRegistry:
    @staticmethod
    def get_scenario_class(action: str):
        scenario_class = SCENARIO_REGISTRY.get(action)
        if not scenario_class:
            raise ValueError(f"Unknown action: {action}")
        return scenario_class


class ScenarioFactory:
    @staticmethod
    def for_profile(profile):
        """Return the scenario class for the profile's current day."""
        action = f"day_{profile.current_day}"
        return TaskRegistry.get_scenario_class(action)
