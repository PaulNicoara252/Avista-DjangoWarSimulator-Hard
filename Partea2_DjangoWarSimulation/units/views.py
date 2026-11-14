from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import FighterForm, FightForm
from .hard_services import run_full_hard_pipeline, simulate_all_api_fights
from .models import Fight, FightTurn, Fighter, Person
from .services import simulate_fight


def create_unit(request):
    if request.method == "POST":
        form = FighterForm(request.POST)
        if form.is_valid():
            fighter = form.save()
            messages.success(request, f"{fighter.name} joined the {fighter.get_team_display()} team.")
            return redirect("units:all")
    else:
        form = FighterForm()
    return render(request, "units/create_unit.html", {"form": form})


def unit_list(request):
    fighters = Fighter.objects.all()
    fight_form = FightForm()
    team_stats = team_statistics()
    return render(
        request,
        "units/unit_list.html",
        {"fighters": fighters, "fight_form": fight_form, "team_stats": team_stats},
    )


@require_POST
def heal_unit(request, pk):
    fighter = get_object_or_404(Fighter, pk=pk)
    if fighter.health < 100:
        fighter.health = 100
        fighter.save(update_fields=["health"])
        messages.success(request, f"{fighter.name} has been healed to full health.")
    return redirect("units:all")


@require_POST
def start_fight(request):
    form = FightForm(request.POST)
    if form.is_valid():
        fight = simulate_fight(form.cleaned_data["first_fighter"], form.cleaned_data["second_fighter"])
        return redirect("units:fight_detail", pk=fight.pk)

    fighters = Fighter.objects.all()
    return render(
        request,
        "units/unit_list.html",
        {"fighters": fighters, "fight_form": form, "team_stats": team_statistics()},
        status=400,
    )


def unit_detail(request, pk):
    fighter = get_object_or_404(Fighter, pk=pk)
    fights = (
        Fight.objects.filter(Q(first_fighter=fighter) | Q(second_fighter=fighter))
        .select_related("first_fighter", "second_fighter", "winner")
        .prefetch_related("turns__attacker", "turns__defender")
    )
    return render(request, "units/unit_detail.html", {"fighter": fighter, "fights": fights})


def fight_detail(request, pk):
    fight = get_object_or_404(
        Fight.objects.select_related("first_fighter", "second_fighter", "winner"), pk=pk
    )
    turns = FightTurn.objects.filter(fight=fight).select_related("attacker", "defender")
    return render(request, "units/fight_detail.html", {"fight": fight, "turns": turns})


def simulate_page(request):
    stats = None
    if request.method == "POST":
        action = request.POST.get("action", "replay")
        if action == "resync":
            stats = run_full_hard_pipeline(force_resync=True)
            messages.success(
                request,
                f"Synced {stats['people_count']} people, drafted {stats['drafted_count']} soldiers, and replayed {stats['replayed_count']} fights in parallel ({stats['elapsed_seconds']}s).",
            )
        else:
            stats = simulate_all_api_fights(reset_health=True, max_workers=8)
            messages.success(
                request,
                f"Replayed {stats['replayed_count']} fights in parallel across {stats['workers']} workers in {stats['elapsed_seconds']}s.",
            )
        return redirect("units:hard_simulate")

    if not Fight.objects.filter(external_fight_id__isnull=False, is_simulated=True).exists():
        stats = run_full_hard_pipeline(force_resync=False)

    api_fights = (
        Fight.objects.filter(external_fight_id__isnull=False)
        .select_related("first_fighter", "second_fighter", "winner")
        .prefetch_related("turns__attacker", "turns__defender")
        .order_by("external_fight_id")
    )
    drafted_soldiers = Fighter.objects.filter(external_id__isnull=False).order_by("external_id")

    return render(
        request,
        "units/simulate.html",
        {
            "api_fights": api_fights,
            "drafted_soldiers": drafted_soldiers,
            "people_count": Person.objects.count(),
            "stats": stats,
            "team_stats": team_statistics(),
        },
    )


def team_statistics():
    rows = []
    for team, label in Fighter.Team.choices:
        caused = FightTurn.objects.filter(attacker__team=team).aggregate(total=Sum("damage"))["total"] or 0
        taken = FightTurn.objects.filter(defender__team=team).aggregate(total=Sum("damage"))["total"] or 0
        fights = (
            Fight.objects.filter(first_fighter__team=team) | Fight.objects.filter(second_fighter__team=team)
        ).distinct().count()
        rows.append({"team": label, "fights": fights, "damage_caused": caused, "damage_taken": taken})
    return rows