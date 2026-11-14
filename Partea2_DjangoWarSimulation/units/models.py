from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Person(models.Model):
    external_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=100)
    age = models.PositiveSmallIntegerField()
    rank = models.CharField(max_length=32, blank=True, default="")
    is_drafted = models.BooleanField(default=False)

    class Meta:
        ordering = ["external_id"]

    def __str__(self):
        return f"#{self.external_id} {self.name} ({self.rank or 'Unranked'})"


class Fighter(models.Model):
    class UnitType(models.TextChoices):
        INFANTRY = "infantry", "Infantry"
        CANNON = "cannon", "Cannon"
        CAVALRY = "cavalry", "Cavalry"
        KNIGHT = "knight", "Knight"

    class Team(models.TextChoices):
        BLUE = "blue", "Blue"
        RED = "red", "Red"

    external_id = models.PositiveIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=100)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    rank = models.CharField(max_length=32, blank=True, default="")
    unit_type = models.CharField(max_length=16, choices=UnitType.choices)
    team = models.CharField(max_length=8, choices=Team.choices)
    health = models.PositiveSmallIntegerField(
        default=100, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    attack_points = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(50)]
    )
    profile_image_link = models.URLField()

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(health__gte=0) & models.Q(health__lte=100),
                name="fighter_health_range",
            ),
            models.CheckConstraint(
                condition=models.Q(attack_points__gte=0) & models.Q(attack_points__lte=50),
                name="fighter_attack_range",
            ),
        ]

    def __str__(self):
        rank_part = f" [{self.rank}]" if self.rank else ""
        return f"{self.name}{rank_part} ({self.get_team_display()} {self.get_unit_type_display()})"


class Fight(models.Model):
    external_fight_id = models.PositiveIntegerField(unique=True, null=True, blank=True)
    first_fighter = models.ForeignKey(Fighter, on_delete=models.PROTECT, related_name="fights_started")
    second_fighter = models.ForeignKey(Fighter, on_delete=models.PROTECT, related_name="fights_received")
    winner = models.ForeignKey(
        Fighter, null=True, blank=True, on_delete=models.SET_NULL, related_name="won_fights"
    )
    is_stalemate = models.BooleanField(default=False)
    is_simulated = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["external_fight_id", "-created_at", "-pk"]

    def __str__(self):
        label = f"API #{self.external_fight_id}" if self.external_fight_id is not None else f"#{self.pk}"
        return f"Fight {label}: {self.first_fighter.name} vs {self.second_fighter.name}"


class FightEvent(models.Model):
    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="events")
    step_number = models.PositiveIntegerField()
    attacker = models.ForeignKey(Fighter, on_delete=models.PROTECT, related_name="scheduled_attacks")
    defender = models.ForeignKey(Fighter, on_delete=models.PROTECT, related_name="scheduled_defenses")
    damage = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["step_number"]
        constraints = [
            models.UniqueConstraint(fields=["fight", "step_number"], name="unique_event_per_fight")
        ]

    def __str__(self):
        return f"Fight #{self.fight_id} event {self.step_number}: {self.attacker_id} -> {self.defender_id} ({self.damage})"


class FightTurn(models.Model):
    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="turns")
    turn_number = models.PositiveIntegerField()
    attacker = models.ForeignKey(Fighter, on_delete=models.PROTECT, related_name="attacks_made")
    defender = models.ForeignKey(Fighter, on_delete=models.PROTECT, related_name="attacks_received")
    damage = models.PositiveSmallIntegerField()
    attacker_health = models.PositiveSmallIntegerField()
    defender_health = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["turn_number"]
        constraints = [models.UniqueConstraint(fields=["fight", "turn_number"], name="unique_turn_per_fight")]

    def __str__(self):
        return f"Fight #{self.fight_id}, turn {self.turn_number}"