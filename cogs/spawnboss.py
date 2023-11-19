import asyncio
import random
import sys
from datetime import datetime, timedelta
from logging import DEBUG

import discord
from discord.ext import commands
from enum import Enum

from functions.functions_boss import fRandomBossHp

sys.path.insert(1, './functions/')
import functions_boss
import functions_database
import functions_general
import functions_modifiers
import functions_pets
import functions_daily
import functions_expsum
import functions_events
import functions_skills
import functions_patrons
import functions_achievements
import functions_tower
import functions_twitch

#Import Globals
from globals.globalvariables import DebugMode

#Respawn time
global respTime
respTime = 0

#Boss alive
global BOSSALIVE
BOSSALIVE = 0

#Shrine alive
global EVENT_ALIVE
EVENT_ALIVE = 0

#Define event type enum
class EventType(Enum):
    NONE = 0
    SHRINE = 1
    CHEST = 2
    INVASION = 3
    PARTY = 4
    MEMORY = 5
    HUNTING = 6

global EVENT_TYPE
EVENT_TYPE = EventType.NONE

#Boss rarity
global BOSSRARITY
BOSSRARITY = 0

#Bot is busy
global BUSY
BUSY = 0

class message(commands.Cog, name="spawnBoss"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Every week ranking check and select the boss slayer."""

        #Choose channel to spawn boss
        global ctx
        if DebugMode is True:
            ctx = await functions_boss.getContext(self, 970571647226642442, 1125837611299254383)
        else:
            ctx = await functions_boss.getContext(self, 970684202880204831, 1028328642436136961)

        global BOSSALIVE, BOSSRARITY, respTime, respawnResume
        bossRar, respawnTime, respawnResume = await functions_database.readBossTable(self, ctx)

        #Weekly ranking task create
        self.task2 = self.bot.loop.create_task(self.weekly_ranking(ctx))

        #Chest spawn task create
        self.task3 = self.bot.loop.create_task(self.spawn_event(ctx))

        #Check if it is necessary to resume boss spawn
        print("Resume?: " + str(respawnResume))
        if respawnResume is True:
            BOSSALIVE = 1
            BOSSRARITY = int(bossRar)
            try:
                respTime = (respawnTime - (datetime.utcnow() + timedelta(hours=2))).total_seconds()
            except:
                respTime = 0
            print("Resp time: " + str(respTime))
            print("Task resuming...")
            self.task = self.bot.loop.create_task(self.spawn_task(ctx))
            print("Task resumed.")

    #define every week task
    # 7 days => 24 hour * 7 days = 168
    async def weekly_ranking(self, ctx):
        while True:
            timestamp = (datetime.utcnow() + timedelta(hours=2))
            if timestamp.strftime("%H:%M UTC %a") == "15:00 UTC Mon":
                print('Weekly ranking summary!')
                winnerID = await functions_database.readSummaryRankingTable(self, ctx)
                print("Winner ID: " + str(winnerID))
                await functions_boss.setBossSlayer(self, ctx, winnerID)

                # Achievements - Butcher
                guild = self.bot.get_guild(686137998177206281)
                winner = guild.get_member(int(winnerID))
                await functions_achievements.add_butcher(self, ctx, winner)

                await functions_database.resetRankingTable(self)
                await functions_daily.clear_daily_file(self)
                await functions_tower.clear_weekly_tower_file(self)
                await ctx.channel.send("<@&985071779787730944>! Ranking za tydzień polowań został zresetowany. Nowa rola <@&983798433590673448> została przydzielona <@" + str(winnerID) + ">! Gratulacje <:GigaChad:970665721321381958>")
                return
            if timestamp.strftime("%H:%M UTC") == "04:10 UTC":
                await functions_daily.clear_daily_file(self)

            if timestamp.strftime("%H:%M UTC") == "20:30 UTC" or \
                timestamp.strftime("%H:%M UTC") == "5:30 UTC":
                await functions_twitch.assign_roles_messages(self)
                await functions_twitch.assign_roles_watchtime(self)
                await functions_twitch.check_treasure(self)

            # wait some time before another loop. Don't make it more than 60 sec or it will skip
            await asyncio.sleep(35)

    async def spawn_event(self, ctx):
        """Spawns an event.."""

        print("Spawning event task starting...")
        global EVENT_ALIVE, EVENT_TYPE, BOSSALIVE, BUSY
        EVENT_ALIVE = 0
        EVENT_TYPE = EventType.NONE

        while True:
            if DebugMode is False:
                resp_time = random.randint(1200, 2500)
            elif DebugMode is True:
                resp_time = random.randint(15, 20)

            await asyncio.sleep(resp_time)
            EVENT_ALIVE = 0
            EVENT_TYPE = EventType.NONE

            timestamp = (datetime.utcnow() + timedelta(hours=2))
            hour = timestamp.strftime("%H")
            day = timestamp.strftime("%a")

            event_list = [EventType.SHRINE, EventType.CHEST, EventType.INVASION, EventType.PARTY,
                          EventType.MEMORY, EventType.HUNTING]
            if (hour == "18" or hour == "19" or hour == "20" or hour == "21") and not DebugMode:
                EVENT_TYPE = random.choices(event_list, weights=(1, 1, 2, 1, 1, 1))[0]
            else:
                EVENT_TYPE = random.choices(event_list, weights=(2, 2, 1, 0, 2, 2))[0]
                #EVENT_TYPE = random.choices(event_list, weights=(0, 0, 0, 0, 0, 2))[0]

            print("Event type: " + str(EVENT_TYPE))
            if EVENT_ALIVE == 0 and (BOSSALIVE == 0 or BOSSALIVE == 1 or BOSSALIVE == 2) and\
                BUSY == 0:
                await functions_general.fClear(self, ctx)
                if EVENT_TYPE == EventType.SHRINE:
                    await functions_modifiers.spawn_modifier_shrine(self, ctx)
                    EVENT_ALIVE = 1
                elif EVENT_TYPE == EventType.CHEST:
                    await functions_events.spawn_chest(self, ctx)
                    EVENT_ALIVE = 1
                elif EVENT_TYPE == EventType.MEMORY:
                    EVENT_ALIVE = 1
                    BUSY = 1
                    await functions_events.spawn_memory(self, ctx)
                    EVENT_ALIVE = 0
                    BUSY = 0
                elif EVENT_TYPE == EventType.INVASION:
                    EVENT_ALIVE = 1
                    BUSY = 1
                    await functions_events.spawn_invasion(self, ctx)
                    EVENT_ALIVE = 0
                    BUSY = 0
                    print("Invasion ended.")
                elif EVENT_TYPE == EventType.PARTY:
                    EVENT_ALIVE = 1
                    BUSY = 1
                    await functions_events.spawn_party(self, ctx)
                    EVENT_ALIVE = 0
                    BUSY = 0
                elif EVENT_TYPE == EventType.HUNTING:
                    EVENT_ALIVE = 1
                    BUSY = 1
                    await functions_events.spawn_hunting(self, ctx)
                    EVENT_ALIVE = 0
                    BUSY = 0
            elif BOSSALIVE > 2:
                print("Boss spawned. Skip.")
            elif BUSY == 1:
                print("Bot busy, skip.")
            else:
                print("Unknow state of event.")

    #define Spawn BIG Boss task
    async def spawn_task(self, ctx):
        while True:
            global respTime
            global BOSSALIVE
            global BOSSRARITY
            global EVENT_ALIVE
            global BUSY
            global respawnResume
            #=== Episode 0
            if BOSSALIVE == 0:
                print("Preparing to channel clear. BOSSALIVE = 0")
                BOSSALIVE = 1
                if DebugMode is False:
                    respTime = random.randint(150,3600)*12

                    BOSSRARITY = functions_boss.fBOSSRARITY(respTime)
                    await functions_database.updateBossTable(self, ctx, BOSSRARITY, respTime, True)

                    print("Resp time: " + str(respTime))
                    print("Boss Rarity: " + str(BOSSRARITY))
                    await asyncio.sleep(3600)
                else:
                    respTime = random.randint(15,24)

                    BOSSRARITY = functions_boss.fBOSSRARITY(respTime)
                    print("Updating database...")
                    await functions_database.updateBossTable(self, ctx, BOSSRARITY, respTime, True)

                    print("Resp time: " + str(respTime))
                    print("Boss Rarity: " + str(BOSSRARITY))
                    await asyncio.sleep(3)
            #=== Episode 1 - Waiting
            #New Spawn
            if respawnResume is False:
                if BOSSALIVE == 1:
                    BOSSALIVE = 2
                    await functions_general.fClear(self, ctx)
                    print("Channel cleared. BOSSALIVE = 1")
                    async with ctx.typing():
                        await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')

                    await asyncio.sleep(respTime)  # time in seconds

            #Resume Spawn
            else:
                if BOSSALIVE == 1:
                    BOSSALIVE = 2

                    wait_loop = 0
                    while BUSY == 1 and wait_loop <= 60:
                        print("Waiting in loop, beacuse bot BUSY")
                        await asyncio.sleep(15)
                        wait_loop += 1
                    await functions_general.fClear(self, ctx)
                    print("Channel cleared. BOSSALIVE = 1. Resuming.")
                    print("Resume resp time: " + str(respTime))
                    print("Resume boss Rarity: " + str(BOSSRARITY))
                    async with ctx.typing():
                        await ctx.channel.send('Dookoła rozlega się cisza, jedynie wiatr wzbija w powietrze tumany kurzu...')
                    await asyncio.sleep(respTime)  # time in seconds

            #=== Episode 2 - Before fight
            if BOSSALIVE == 2:
                BOSSALIVE = 3

                # Load all modifiers from file
                modifiers = await functions_modifiers.load_modifiers(self, ctx)
                BOSSRARITY += modifiers["rarity_boost"]
                if BOSSRARITY > 4:
                    BOSSRARITY = 4

                # If early hour then reduce boss rarity to epic
                timestamp = (datetime.utcnow() + timedelta(hours=2))
                hour = timestamp.strftime("%H")
                day = timestamp.strftime("%a")

                if BOSSRARITY == 3 and not (hour == "16" or hour == "17" or hour == "18" or hour == "19" or hour == "20" or hour == "21" or hour == "22") and not (day == "Sun" or day == "Sat") and not DebugMode:
                    BOSSRARITY = 4

                # Detection skill
                await functions_pets.detect_boss(self, BOSSRARITY)
                print("After dection.")
                #Channel Clear
                print("Channel cleared. BOSSALIVE = 2")
                async with ctx.typing():
                    await ctx.channel.send('Wiatr wzmaga się coraz mocniej, z oddali słychać ryk, a ziemią targają coraz mocniejsze wstrząsy... <:MonkaS:882181709100097587>')
                if DebugMode is False:
                    await asyncio.sleep(random.randint(4,7)*60)  # time in second
                else:
                    await asyncio.sleep(random.randint(3,10))  # time in second

            #=== Episode 3 - Boss respawn
            if BOSSALIVE == 3:
                print("Channel cleared.")
                EVENT_ALIVE = 0
                BUSY = 0
                await functions_general.fClear(self, ctx)
                print("Boss appeared.")
                #Send info about boss spawn
                try:
                    await generalSpawnMessage.delete()
                    print("Message deleted.")
                except:
                    print("No general message to delete.")
                if DebugMode is False:
                    chatChannel = self.bot.get_channel(696932659833733131)
                    generalSpawnMessage = await chatChannel.send("Na kanale <#970684202880204831> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!")
                else:
                    chatChannel = self.bot.get_channel(881090112576962560)
                    generalSpawnMessage = await chatChannel.send("Na kanale <#970684202880204831> pojawił się właśnie potwór! Zabijcie go, żeby zgarnąć nagrody!")
                #Send boss image based on rarity
                global initCommand, is_player_boss, player_boss
                initCommand = "zaatakuj"
                is_player_boss, player_boss = await functions_boss.fBossImage(self, ctx, BOSSRARITY)
                BOSSALIVE = 4
            else:
                await asyncio.sleep(5) #sleep for a while

    #pray to the shrine command
    @commands.command(name="modlitwa", brief="Patron or Creator can pray to the shrine.")
    async def ShrinePray(self, ctx):
        print("Praying...")
        global EVENT_ALIVE, EVENT_TYPE

        if EVENT_ALIVE == 1 and EVENT_TYPE == EventType.SHRINE:
            EVENT_ALIVE = 0

            if functions_patrons.check_if_patron(self, ctx, ctx.author):
                await ctx.message.add_reaction("<:prayge:1063891597760139304>")
                await functions_modifiers.random_modifiers(self, ctx, True)
            else:
                await ctx.channel.send("Nie umiesz pacierza. Poczekaj na kogoś bardziej wierzącego. <:prayge:1063891597760139304>")
                EVENT_ALIVE = 1
        else:
            await ctx.channel.send("Do kogo Ty chcesz się modlić? Przecież tu nic nie ma...")

    # Open a chest command
    @commands.command(name="skrzynia", brief="Everyone can open a chest.")
    async def OpenChest(self, ctx):
        print("Opening chest...")
        global EVENT_ALIVE, EVENT_TYPE

        if EVENT_ALIVE == 1 and EVENT_TYPE == EventType.CHEST:
            EVENT_ALIVE = 0
            await functions_events.open_chest(self, ctx)
        else:
            await ctx.channel.send("Powiedz mi... Czy ta skrzynia jest tutaj z nami w pokoju? <:Hmm:984767035617730620>")

    # command to attack the boss - rarity 0, 1, 2
    @commands.command(pass_context=True, name="zaatakuj", brief="Attacking the boss")
    async def attackMessage(self, ctx):
        global BOSSALIVE, BOSSRARITY, respawnResume

        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:

            if BOSSALIVE == 4: #or str(ctx.message.author.id) == '291836779495948288':
                BOSSALIVE = 5

                if BOSSRARITY in [0,1,2,4]:
                    BOSSALIVE, bossHunterID = await functions_boss.singleInit(self, ctx,
                                                                              BOSSALIVE, BOSSRARITY)
                    BOSSALIVE = await functions_boss.singleFight(self, ctx, BOSSALIVE,
                                                                 bossHunterID, BOSSRARITY,
                                                                 is_player_boss, player_boss)
                elif BOSSRARITY == 3:
                    BOSSALIVE, playersList = await functions_boss.groupInit(self, ctx,
                                                                            BOSSALIVE, BOSSRARITY)
                    print(BOSSALIVE)
                    BOSSALIVE, playersList = await functions_boss.groupFight(self, ctx,
                                                                             BOSSALIVE, playersList,
                                                                             is_player_boss,
                                                                             player_boss)
            elif BOSSALIVE == 5:
                pass
            else:
                #Boss not Alive
                print("Boss is not alive or attacked!")
                await ctx.channel.send('Nie możesz zaatakować bossa, poczekaj na pojawienie się kolejnego <@' + format(ctx.message.author.id) + '>!')
        else:
            pass #wrong channel


    # ==================================== COMMANDS FOR USERS =======================================================================

    # command to check boss kill record
    @commands.command(name="rekord")
    async def rekord(self, ctx):
        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:
            global BUSY
            if BUSY == 0:
                recordTime, Nick = await functions_database.readRecordTable(self, ctx)
                print ("Record database read.")
                await ctx.channel.send('Poprzedni rekord należy do **' + Nick + '** i wynosi średnio **' + recordTime.lstrip('00:') + ' sekundy na turę walki**.')

    # command to check last boss kill
    @commands.command(pass_context=True, name="kiedy", brief="Check previous boss kill time")
    async def lastKillInfoMessage(self, ctx):
        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:
            global BUSY
            if BUSY == 0:
                fightTime, Nick = await functions_database.readHistoryTable(self, ctx)
                print ("History database read.")
                await ctx.channel.send('Poprzednio boss walczył z **' + Nick + '** i było to **' + fightTime[:16] + ' UTC+1**.')

    @commands.command(name="ranking", aliases=['r'],)
    async def readRankingDatabase(self, ctx):
        if ctx.channel.id == 970684202880204831 or ctx.channel.id == 970571647226642442:
            global BUSY
            if BUSY == 0:
                await functions_database.readRankingTable(self, ctx)
                print ("Ranking database read.")

    @commands.command(name="towarzysz", aliases=['t'], brief="Shows author's pet.")
    async def show_pet(self, ctx):
        global BUSY
        if BUSY == 0:
            await functions_pets.show_pet(self, ctx, ctx.author)

    @commands.command(name="porzucam", brief="Discards author's pet.")
    async def discard_pet(self, ctx):
        print("Discarding author's pet")
        global BUSY
        if BUSY == 0:
            await functions_pets.discard_pet(self, ctx)

    @commands.command(name="odrodzenie", aliases=['odr', 'o'], brief="Reroll the pet.")
    async def reroll_pet(self, ctx):
        global BUSY
        if BUSY == 0:
            await functions_pets.reroll_pet(self, ctx, ctx.author)

    @commands.command(name="mocneodrodzenie", aliases=["odrodzenie2", "odr2", "mocneodr", "mo"],
                      brief="Advanced reroll the pet.")
    async def adv_reroll_pet(self, ctx):
        global BUSY
        BUSY = 1
        await functions_pets.adv_reroll_pet(self, ctx)
        BUSY = 0

    @commands.command(name="oswiecenie", aliases=['oświecenie'], brief="Enlight the pet.")
    async def enlight_pet(self, ctx):
        global BUSY
        if BUSY == 0:
            await functions_pets.enlight_pet(self, ctx, ctx.author)

    @commands.command(name="transformacja", aliases=['transform'], brief="Transform the pet.")
    async def transform_pet(self, ctx):
        global BUSY
        if BUSY == 0:
            await functions_pets.transform_pet(self, ctx, ctx.author)

    @commands.command(name="schowajtowarzysza",
                      aliases=["schowaj", "st"], brief="Stores author's pet.")
    async def store_pet(self, ctx, slot):
        global BUSY
        if BUSY == 0:
            await functions_pets.store_pet(self, ctx, slot)

    @store_pet.error
    async def storepet_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Po spacji podaj numer miejsca w stajni, do którego chcesz schować " +
                           "towarzysza (1 lub 2) np. **$schowajtowarzysza 1**.")
    
    @commands.command(name="wyciagnijtowarzysza",
                      aliases=['wyciągnijtowarzysza', "wyciągnij", "wyciagnij", "wt"],
                      brief="Stores author's pet.")
    async def unstore_pet(self, ctx, slot):
        global BUSY
        if BUSY == 0:
            await functions_pets.unstore_pet(self, ctx, slot)

    @unstore_pet.error
    async def unstorepet_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Po spacji podaj numer miejsca w stajni, z którego chcesz wyjąć " +
                           "towarzysza (1 lub 2) np. **$wyciagnijtowarzysza 1**.")

    @commands.command(name="stajnia", aliases=["s"], brief="Show player's stable.")
    async def check_stable(self, ctx):
        global BUSY
        if BUSY == 0:
            await functions_pets.check_stable(self, ctx)

    @commands.command(name="rankingtowarzyszy", aliases=['towarzysze', 'rt'],
                      brief="Shows pets ranking.")
    async def pet_ranking(self, ctx):
        global BUSY
        if BUSY == 0:
            await functions_pets.pet_ranking(self, ctx)

    @commands.command(name="nazwij", aliases=['n'], brief="Set the name of author's pet.")
    @commands.cooldown(1, 60*60*23, commands.BucketType.user)
    async def name_pet(self, ctx, name):
        global BUSY
        if BUSY == 0:
            await functions_pets.name_pet(self, ctx, name)

    @name_pet.error
    async def addfantasy_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    @commands.command(name="polowanie", aliases=['pol', 'p'], brief="Try to hunt on a mobs.")
    async def hunting(self, ctx):
        global BOSSALIVE, BUSY, DebugMode
        on_cd = functions_daily.load_daily_from_file(ctx.author.id)
        if (BOSSALIVE in [0,1,2] or DebugMode is True) and BUSY == 0 and not on_cd:
            BUSY = 1
            functions_daily.save_daily_to_file(ctx.author.id)
            rarity = random.randint(0,100)
            if 0 <= rarity < 70:
                rarity = 0
            elif 70 <= rarity <= 98:
                rarity = 1
            else:
                rarity = 2
            is_player_boss, boss_player = await functions_daily.fBossImage(self, ctx, rarity)
            await functions_daily.hunt_mobs(self, ctx, rarity, is_player_boss, boss_player)
            BUSY = 0
        elif BOSSALIVE == 3:
            await ctx.channel.send("Zaraz pojawi się prawidzwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, to wpisz **$zaatakuj**, żeby stawić mu czoła.")
        elif BOSSALIVE == 4:
            await ctx.channel.send("Teraz pora na walkę z prawdziwym bossem, nie mieszaj się leszczyku <:madge:882184635474386974>")
        elif BOSSALIVE > 4:
            pass
        elif BUSY == 1:
            pass
        elif on_cd:
            await ctx.channel.send("Zregeneruj się i spróbuj zapolować jutro <:Bedge:970576892874854400> Niektóre modlitwy przy kapliczkach również są w stanie zregenerować Twoje siły, więc bądź czujny!")

    @commands.command(name="wieża", aliases=['wieza'],
                      brief="Try to explor the death tower.")
    async def explore_tower(self, ctx):
        global BOSSALIVE, BUSY, DebugMode
        on_cd = functions_tower.load_weekly_tower_from_file(ctx.author.id)
        if (BOSSALIVE in [0,1,2] or DebugMode is True) and BUSY == 0 and not on_cd:
            BUSY = 1
            functions_tower.save_weekly_tower_to_file(ctx.author.id)
            iteration = 1
            success_fight = True
            while iteration < 11 and success_fight:
                await functions_tower.tower_image(self, ctx, iteration)
                success_fight = await functions_tower.tower_fight(self, ctx, iteration, ctx.author)
                iteration += 1
            BUSY = 0
        elif BOSSALIVE == 3:
            await ctx.channel.send("Zaraz pojawi się prawidzwe wyzwanie <:MonkaS:882181709100097587> Gdy to się stanie, to wpisz **$zaatakuj**, żeby stawić mu czoła.")
        elif BOSSALIVE == 4:
            await ctx.channel.send("Nie możesz ruszyć na eksplorację Wieży Śmierci, na Twojej drodze stoi boss <:MonkaS:882181709100097587> Wpusz **$zaatakuj**, żeby stawić mu czoła.")
        elif BOSSALIVE > 4:
            pass
        elif BUSY == 1:
            pass
        elif on_cd:
            await ctx.channel.send("Drzwi Wieży Śmierci ani drgną. Sprawdź w następnym tygodniu <:Bedge:970576892874854400>")

    @commands.command(name="rankingwiezy", aliases=['wieze', 'rankingwieza', 'rankingwieży',
                                                    'rankingwieża', 'rw'],
                      brief="Shows death tower ranking.")
    async def tower_ranking(self, ctx):
        await functions_tower.tower_ranking(self, ctx)

    @commands.command(name="rekordwiezy", aliases=["rekordwieży", "rekordwieża", "rekordwieza"],
                      brief="Shows personal death tower record.")
    async def check_personal_tower(self, ctx):
        await functions_tower.check_personal_tower(self, ctx)

    # command to flex boss slayer
    @commands.command(pass_context=True, name="flex", brief="Boss slayer flex")
    @commands.cooldown(1, 1800, commands.BucketType.user)
    async def flex(self, ctx):
        my_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        print(str(type(ctx.message.author)))
        if my_role in ctx.message.author.roles:
            await functions_boss.flexGif(self, ctx)
            await ctx.channel.send('Potężny <:GigaChad:970665721321381958> <@' + format(ctx.message.author.id) + '> napina swe sprężyste, naoliwione muskuły! Co za widok, robi wrażenie! <:pogu:882182966372106280>')
        else:
            await ctx.channel.send("""
            <:KEKW:936907435921252363> **Miernota** <:2Head:882184634572627978>""")

    @flex.error
    async def flexcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    # command to change color
    @commands.command(pass_context=True, name="kolor", brief="Boss slayer color change")
    @commands.cooldown(1, 1800, commands.BucketType.user)
    async def kolor(self, ctx, hexColor):
        my_role = discord.utils.get(ctx.guild.roles, id=983798433590673448)
        print(str(type(ctx.message.author)))
        if my_role in ctx.message.author.roles:
            print("Before function to change color.")
            await functions_boss.changeColor(self, ctx, hexColor)
        else:
            await ctx.channel.send('<:KEKW:936907435921252363> **Kpisz sobie, miernoto?** <:2Head:882184634572627978>')

    @kolor.error
    async def flexcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    # command to change icon
    @commands.command(pass_context=True, name="ikona", brief="Boss slayer icon change")
    @commands.cooldown(1, 1800, commands.BucketType.user)
    async def changeIcon(self, ctx):
        print("Before function to change color.")
        await functions_boss.changeIcon(self, ctx)

    @changeIcon.error
    async def flexcommand_cooldown(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            print("Command on cooldown.")
            await ctx.send('Poczekaj na odnowienie komendy! Zostało ' + str(round(error.retry_after/60/60, 2)) + ' godzin/y <:Bedge:970576892874854400>.')

    # Command to check the exp summary and reset it.
    @commands.command(pass_context=True, name="Exp")
    @commands.has_permissions(administrator=True)
    async def exp_summary(self, ctx):
        """Show the exp summary."""

        await functions_expsum.show_file_exp(self, ctx)
        functions_expsum.init_file_exp(self)
    # ==================================== COMMANDS FOR DEBUG ======================================

    #create Spawn Boss task command
    @commands.command(name="startSpawnBoss", brief="Starts spawning boss")
    @commands.has_permissions(administrator=True)
    async def startMessage(self, ctx):
        print("Spawning started!")
        global BOSSALIVE
        BOSSALIVE = 0
        self.task = self.bot.loop.create_task(self.spawn_task(ctx))

    # command to stop Spawn Boss task
    @commands.command(pass_context=True, name="stopSpawnBoss", brief="Stops spawning boss")
    @commands.has_permissions(administrator=True)
    async def stopMessage(self, ctx):
        print("Spawning stopped!")
        global BOSSALIVE, respawnResume
        respawnResume = False
        BOSSALIVE = 0
        await functions_database.updateBossTable(self, ctx, 0, 0, False)
        self.task.cancel()

    # command to check Spawn Boss
    @commands.command(pass_context=True, name="checkSpawnBoss", brief="Checking boss spawn time")
    @commands.has_permissions(administrator=True)
    async def checkSpawnMessage(self, ctx):
        await ctx.channel.send("Resp time is " + str(respTime/60/60) + " hours.")

    # command to debug
    @commands.command(pass_context=True, name="AddColumn")
    @commands.has_permissions(administrator=True)
    async def add_column(self, ctx):
        # sql ='''ALTER TABLE PETOWNER
        # DROP COLUMN REBIRTH_STONES;
        # '''
        #await self.bot.pg_con.execute(sql)

        sql ='''ALTER TABLE PETOWNER
        ADD SKILL_GEM NUMERIC DEFAULT 0;
        '''
        await self.bot.pg_con.execute(sql)

        sql ='''ALTER TABLE PETOWNER
        ADD SKILL_GEM NUMERIC DEFAULT 0;
        '''
        await self.bot.pg_con.execute(sql)

    # command to debug
    @commands.command(pass_context=True, name="SpawnParty", brief="Spawn a party.")
    @commands.has_permissions(administrator=True)
    async def spawn_party(self, ctx):

        await functions_events.spawn_party(self, ctx)

    # command to debug
    @commands.command(pass_context=True, name="SpawnMemory", brief="Spawn a memory game.")
    @commands.has_permissions(administrator=True)
    async def spawn_memory(self, ctx):
        await functions_events.spawn_memory(self, ctx)

    # command to debug
    @commands.command(pass_context=True, name="SpawnShrine", brief="Spawn a shrine.")
    @commands.has_permissions(administrator=True)
    async def spawn_shrine(self, ctx):
        global EVENT_ALIVE, EVENT_TYPE
        EVENT_TYPE = EventType.SHRINE
        EVENT_ALIVE = 1
        await functions_modifiers.spawn_modifier_shrine(self, ctx)

    # command to debug
    @commands.command(pass_context=True, name="PetLvlUp", brief="Level up pet of the player id.")
    @commands.has_permissions(administrator=True)
    async def level_up_pet(self, ctx, player_id):
        await functions_pets.level_up_pet(self, ctx, player_id)

    # command to debug
    @commands.command(pass_context=True, name="AddScroll")
    @commands.has_permissions(administrator=True)
    async def add_scroll(self, ctx, number, player_id):
        await functions_pets.assign_scroll(self, ctx, number, player_id)

    # command to debug
    @commands.command(pass_context=True, name="AddShard")
    @commands.has_permissions(administrator=True)
    async def add_shard(self, ctx, number, player_id):
        await functions_pets.assign_shard(self, ctx, number, player_id)

    # command to debug
    @commands.command(pass_context=True, name="ClearCD")
    @commands.has_permissions(administrator=True)
    async def clear_cd(self, ctx):
        await functions_daily.clear_daily_file(self)
        await ctx.channel.send("Zresetowano daily cd.")

    # command to debug
    @commands.command(pass_context=True, name="ClearTowerCD")
    @commands.has_permissions(administrator=True)
    async def clear_cd(self, ctx):
        await functions_tower.clear_weekly_tower_file(self)
        await ctx.channel.send("Zresetowano tower cd.")

    # command to debug
    @commands.command(pass_context=True, name="time")
    @commands.has_permissions(administrator=True)
    async def checkTime(self, ctx):
        timestamp = (datetime.utcnow() + timedelta(hours=2))
        await ctx.send(str(timestamp))

    # command to debug
    @commands.command(pass_context=True, name="loot")
    @commands.has_permissions(administrator=True)
    async def randLoot(self, ctx, srarity, BossHunter, boost_percent):
        await functions_boss.randLoot(self, ctx, srarity, BossHunter, boost_percent)

    # command to debug
    @commands.command(pass_context=True, name="rarity")
    @commands.has_permissions(administrator=True)
    async def BOSSRARITY(self, ctx, time):
        await ctx.channel.send(str(functions_boss.fBOSSRARITY(time)))

    # command to debug
    @commands.command(pass_context=True, name="image")
    @commands.has_permissions(administrator=True)
    async def bossImage(self, ctx, rarity):
        await functions_boss.fBossImage(self, ctx, rarity)

    # command to debug
    @commands.command(pass_context=True, name="ModifiersLoad",
                      brief="Load modifiers and prints values.")
    @commands.has_permissions(administrator=True)
    async def loadModifiers(self, ctx):
        await functions_modifiers.load_modifiers(self, ctx)
        await ctx.channel.send("Modifiers file loaded.")

    # command to debug
    @commands.command(pass_context=True, name="GenerateEgg",
                      brief="Generates egg and print its data.")
    @commands.has_permissions(administrator=True)
    async def generate_egg(self, ctx):
        await functions_pets.generate_pet_egg(self, ctx)

    # command to debug
    @commands.command(pass_context=True, name="GenerateSkill",
                      brief="Generates skill and print its data.")
    @commands.has_permissions(administrator=True)
    async def generate_skill(self, ctx):
        await functions_skills.generate_skill_gem(self, ctx)

    @commands.command(pass_context=True, name="SendPM",
                      brief="Sends private message to user.")
    async def dm(self, ctx):
        #user=await self.bot.get_user_info(291836779495948288)
        #guild = self.bot.get_guild(686137998177206281)
        #user = guild.get_member(int(291836779495948288))
        user = await self.bot.fetch_user(291836779495948288)
        await user.send("Hello there!")

    # command to debug
    @commands.command(name="context")
    @commands.has_permissions(administrator=True)
    async def context(self, ctx):
        await functions_boss.getContext(self)

    # command to reset boss
    @commands.command(pass_context=True, name="ResetBoss")
    @commands.has_permissions(administrator=True)
    async def reset_boss(self, ctx):
        global BOSSALIVE, respawnResume, EVENT_ALIVE
        respawnResume = False
        BOSSALIVE = 0
        EVENT_ALIVE = 0
        await functions_database.updateBossTable(self, ctx, 0, 0, False)
        try:
            self.task.cancel()
        except Exception:
            pass
        try:
            self.task3.cancel()
        except Exception:
            pass
        await functions_modifiers.init_modifiers(self, ctx)
        await functions_boss.resetDeadHunters(self, ctx)

        self.task = self.bot.loop.create_task(self.spawn_task(ctx))
        self.task3 = self.bot.loop.create_task(self.spawn_event(ctx))
        await ctx.channel.send("Boss reset.")


    # ==================================== COMMANDS FOR DATABASE ===================================

    @commands.command(name="updateDatabase")
    @commands.has_permissions(administrator=True)
    async def updateDatabase(self, ctx, BOSSRARITY, respTime, respBool):
        await functions_database.updateBossTable(self, ctx, BOSSRARITY, respTime, respBool)
        await ctx.channel.send("Database updated.")

    @commands.command(name="readDatabase")
    @commands.has_permissions(administrator=True)
    async def readDatabase(self, ctx):
        bossRar, respawnTime, respawnResume = await functions_database.readBossTable(self, ctx)
        print ("Database read.")
        await ctx.channel.send("Czy boss będzie wskrzeszony?: " + str(respawnResume))
        await ctx.channel.send("Boss rarity: " + str(bossRar))
        await ctx.channel.send("Czas wskrzeszenia: " + str(respawnTime))

    @commands.command(name="createAllDatabases")
    @commands.has_permissions(administrator=True)
    async def createAllDatabases(self, ctx):
        await functions_database.createBossTable(self)
        await functions_database.createRecordTable(self)
        await functions_database.createHistoryTable(self)
        await functions_achievements.create_database(self)
        await functions_tower.create_database(self)
        await ctx.channel.send("Wszystkie bazy danych utworzone!")

    @commands.command(name="createBossDatabase")
    @commands.has_permissions(administrator=True)
    async def createBossDatabase(self, ctx):
        await functions_database.createBossTable(self)
        await ctx.channel.send("Baza danych utworzona.")

    @commands.command(name="createPetOwnersDatabase",
                      brief="Create table PETOWNER table with one default record.")
    @commands.has_permissions(administrator=True)
    async def create_pet_owners_table(self, ctx):
        await functions_pets.create_pet_owners_table(self)
        await ctx.channel.send("Baza danych PETOWNERS utworzona.")

    @commands.command(name="createPetsDatabase",
                      brief="Create table PETS table with one default record.")
    @commands.has_permissions(administrator=True)
    async def create_pets_table(self, ctx):
        await functions_pets.create_pets_table(self)
        await ctx.channel.send("Baza danych PETS utworzona.")

    @commands.command(name="createSkillsDatabase",
                      brief="Create table SKILLS table with one default record.")
    @commands.has_permissions(administrator=True)
    async def create_skills_table(self, ctx):
        await functions_skills.create_skills_table(self)
        await ctx.channel.send("Baza danych SKILLS utworzona.")

    @commands.command(name="createTowerDatabase",
                      brief="Create empty table TOWER.")
    @commands.has_permissions(administrator=True)
    async def create_tower_database(self, ctx):
        await functions_tower.create_database(self, ctx)
        await ctx.channel.send("Baza danych TOWER utworzona.")

    @commands.command(name="ReassignPet",
                      brief="Assign pet_id to player_id.")
    @commands.has_permissions(administrator=True)
    async def reassign_pet(self, ctx, pet_id, player_id):
        await functions_pets.reassing_pet(self, pet_id, player_id)
        await ctx.channel.send(f"Pet {pet_id} przypisany do gracza <@{player_id}>.")

    # ====== Record Database Commands to Debug

    @commands.command(name="createRecordDatabase")
    @commands.has_permissions(administrator=True)
    async def createDatabase(self, ctx):
        await functions_database.createRecordTable(self)
        await ctx.channel.send("Baza danych z rekordem utworzona.")

    @commands.command(name="updateRecordDatabase")
    @commands.has_permissions(administrator=True)
    async def updateRecordDatabase(self, ctx, Nick, recordTime_MM_SS_MS):
        await functions_database.updateRecordTable(self, ctx, Nick, recordTime_MM_SS_MS)
        await ctx.channel.send("Baza danych z rekordem zaktualizowana.")

    # ====== History Database Commands to Debug

    @commands.command(name="createHistoryDatabase")
    @commands.has_permissions(administrator=True)
    async def createHistoryDatabase(self, ctx):
        await functions_database.createHistoryTable(self)
        await ctx.channel.send("Baza danych z historią utworzona.")

    @commands.command(name="updateHistoryDatabase")
    @commands.has_permissions(administrator=True)
    async def updateHistoryDatabase(self, ctx, Nick, fightTime):
        await functions_database.updateHistoryTable(self, ctx, Nick, fightTime)
        await ctx.channel.send("Baza danych z historią zaktualizowana.")

    # ====== Ranking Database Commands to Debug

    @commands.command(name="createRankingDatabase")
    @commands.has_permissions(administrator=True)
    async def createRankingDatabase(self, ctx):
        await functions_database.createRankingTable(self)
        await ctx.channel.send("Baza danych z rankingiem utworzona.")

    @commands.command(name="resetRankingDatabase")
    @commands.has_permissions(administrator=True)
    async def resetRankingDatabase(self, ctx):
        await functions_database.resetRankingTable(self)
        await ctx.channel.send("Baza danych z rankingiem zresetowana.")

    @commands.command(name="updateRankingDatabase")
    @commands.has_permissions(administrator=True)
    async def updateRankingDatabase(self, ctx, ID, points):
        print("Starting command...")
        await functions_database.updateRankingTable(self, ctx, ID, points)

    # ====== Achievements Database Commands to Debug

    @commands.command(name="createAchievementsDatabase")
    @commands.has_permissions(administrator=True)
    async def create_database(self, ctx):
        await functions_achievements.create_database(self, ctx)

    # ====== Twitch Discord Database Commands to Debug

    @commands.command(name="createTwitchDatabase",
                    brief="Create empty table twitch_discord.")
    @commands.has_permissions(administrator=True)
    async def create_twitch_discord(self, ctx):
        await functions_twitch.create_ttv_dc_database(self)
        await ctx.channel.send("Baza danych discord_twitch utworzona.")

    @commands.command(name="checkTwitch",
                    brief="Check if the user has connected Twitch account.")
    @commands.has_permissions(administrator=True)
    async def check_twitch(self, ctx, user_id):
        await functions_twitch.get_user_twitch_name(self, ctx, user_id)

    @commands.command(name="getActiveTwitch",
                    brief="Get all users with sufficient watchtime.")
    @commands.has_permissions(administrator=True)
    async def assign_roles_watchtime(self, ctx):
        await functions_twitch.assign_roles_watchtime(self)

    @commands.command(name="getWriterTwitch",
                    brief="Get all users with sufficient messages count.")
    @commands.has_permissions(administrator=True)
    async def assign_roles_messages(self, ctx):
        await functions_twitch.assign_roles_messages(self)

    @commands.command(name="checkTreasure",
                    brief="Get all users who should be rewarded")
    @commands.has_permissions(administrator=True)
    async def check_treasure(self, ctx):
        await functions_twitch.check_treasure(self)

def setup(bot):
    bot.add_cog(message(bot))
