"""File with all events."""

import json
import discord
import random
import asyncio
from discord.ext import commands

from functions import functions_boss
import functions_daily
import functions_patrons
import functions_database
import functions_general
import functions_modifiers

#Import Globals
from globals.globalvariables import DebugMode

class FunctionsEvents(commands.Cog, name="FunctionsEvents"):

    def __init__(self, bot):
        self.bot = bot

    global spawn_chest
    async def spawn_chest(self, ctx):
        """Function to spawn chest."""

        print("Treasure chest spawn.")

        e_title = "<:Loot:912797849916436570> Skrzynia ze skarbami! <:Loot:912797849916436570>"

        e_descr = ('Pojawiła się skrzynia ze skarbami! Jedna osoba może ją otworzyć komendą'
        ' **$skrzynia**!'
        '\n\n*Crafterzy i Patroni mogą wydropić dodatkowe, kosmetyczne przedmioty!*')

        e_color = 0xFCBA03

        e_thumb = 'https://www.altermmo.pl/wp-content/uploads/altermmo-2-112.png'

        image_name = "events/chest/" + str(random.randint(0,4)) + ".png"
        file=discord.File(image_name)

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)
        embed.set_thumbnail(url=e_thumb)

        await ctx.channel.send(file=file)
        await ctx.send(embed=embed)

    global open_chest
    async def open_chest(self, ctx):
        """Function to open chest."""

        rarity = random.randint(0,100)
        if rarity >= 90:
            rarity = 2
        else:
            rarity = 1
        boost_percent = random.randint(0,25)
        await functions_daily.randLoot(self, ctx, rarity, ctx.author, boost_percent)

    global spawn_invasion
    async def spawn_invasion(self, ctx):
        """Function to spawn invasion."""

        print("Invasion spawn.")

        e_title = "<:RIP:912797982917816341> Inwazja na wioskę! <:RIP:912797982917816341>"

        e_descr = ('Pobliska wioska została zaatakowana przez bezdusznych najeźdźców!\n\n'
        '**Zaoferujcie im swoją pomoc zostawiając reakcję pod tym postem.** Im więcej Was będzie,'
        'tym większa szansa, że odpędzicie najeźdćów i odbijecie zrabowane przez nich łupy!'
        '\n\n*Crafterzy i Patroni mogą wydropić dodatkowe, kosmetyczne przedmioty!*')

        e_color = 0xFC0303

        e_thumb = 'https://www.altermmo.pl/wp-content/uploads/altermmo-3-112.png'

        image_name = "events/invasion/" + str(random.randint(0,4)) + ".png"
        file=discord.File(image_name)

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)
        embed.set_thumbnail(url=e_thumb)

        await ctx.channel.send(file=file)
        msg = await ctx.send(embed=embed)

        await msg.add_reaction("⚔️")
        if DebugMode:
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(600)

        users = []
        message = await ctx.channel.fetch_message(msg.id)
        for reaction in message.reactions:
            async for user in reaction.users():
                guild = message.guild
                if guild.get_member(user.id) is not None and user.id != 859729615123251200 and user.id != 971322848616525874 and user not in users:
                    users.append(user)

        chance = random.randint(0,100)

        if chance <= len(users)*10 + 10:
            for user in users:
                rarity = random.randint(0,100)
                if rarity >= 90:
                    rarity = 2
                else:
                    rarity = 1
                boost_percent = len(users) * 10
                print(boost_percent)
                await functions_daily.randLoot(self, ctx, rarity, user, boost_percent)
        else:
            image_name = "events/invasion/5.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            await ctx.send('Niestety nie udało się obronić wioski... Mężczyźni leżą w kałuży krwi, dzieci'
                     ' zostały rozszarpane na kwałki, a kobiety skończą jako kurtyzany. '
                     'Co z Was za bohaterowie? <:PepeKMS:783992337029267487>')

    global spawn_party
    async def spawn_party(self, ctx):
        """Function to spawn party."""

        print("Party spawn.")

        cheerer = functions_patrons.get_patron(self, ctx)

        e_title = f"<:Drink:912798939542061086> Zdrowie {cheerer.name}! <:Drink:912798939542061086>"

        e_descr = ('Witam <@&985071779787730944>! Tutaj Wasz serwerowy Bard, Stasiek mi na imię.'
                   '\nCzy za kimś chodzi **gorzałka**? Może napilibyśmy się za zdrowie jednego'
                   ' z patronów lub crafterów?\n\n'
                   f'**To co? Za zdrowie {cheerer.name}! Zapraszam do zabawy.**'
                   '\n\n*Zostaw reakcję pod postem, jeśli chcesz wziąć udział.*')

        e_color = 0xc9700a

        file = cheerer.avatar_url

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        await ctx.channel.send(file)
        msg = await ctx.send(embed=embed)

        await msg.add_reaction("<:Drink:912798939542061086>")

        if DebugMode:
            await asyncio.sleep(15)
        else:
            await asyncio.sleep(300)

        users = []
        message = await ctx.channel.fetch_message(msg.id)
        for reaction in message.reactions:
            async for user in reaction.users():
                guild = message.guild
                if guild.get_member(user.id) is not None:
                    users.append(user)
            break
        print(users)
        active_users = len(users)
        if active_users > 2:
            async with ctx.typing():
                await asyncio.sleep(2)
                await ctx.channel.send("No dobra, to zaczynamy imprezę <:Drink:912798939542061086>! Przygotujcie się, zaraz podam toast! Wpiszcie go bez spacji, wielkość liter nie ma znaczenia!")
                await asyncio.sleep(5)

            ranking = {}
            for x in range(1,9):
                await ctx.channel.send(f"**KOLEJKA {x}!**")
                cmd_req = random.choice(["Chluśniem, bo uśniem", "Rybka lubi popływać",
                                         "Pierdykniem, bo odwykniem", "No to po maluchu",
                                         "Zdrowie wasze w gardło nasze",
                                         "Wódka to twój wróg, więc lej ją w mordę",
                                         "Poloneza czas zacząć", "Ciach babkę w piach",
                                         "Żeby dla Siwcoka nie starczyło",
                                         "Pijmy, bo szkło nasiąka",
                                         "Zdrowie pięknych pań i nAvesoma mamy",
                                         "Oby nam się","No to po calaku",
                                         "Żeby nam gęby szuwarami nie zarosły",
                                         "Uchyl się duszo, bo ulewa idzie",
                                         "Człowiek nie wielbłąd, pić musi",
                                         "Pijemy, aż oślepniemy",
                                         "Robota to głupota, picie to jest życie",
                                         "Morda się smieje, gdy piwo się leje",
                                         "Od pieluchy aż po grób, zawsze piwko sobie kup",
                                         "Za tych, którzy nie mogą",
                                         "Za tych, co w kosmosie",
                                         "Nic tak życia nie upiększa, jak jabola dawka większa",
                                         "No to po szklanie i na grindowanie",
                                         "Życie jest krótkie, a pić się chce",
                                         "Pić i jebać, byle z biedy się wygrzebać",
                                         "Za wyruchanych przez Blizzarda",
                                         "Za zakola Alterowskie",
                                         "Żeby Varrakas porzucił anime",
                                         "Za lambo Szakalakela",
                                         "Za sugar daddyego Sanczo",
                                         "Żeby widzowie na Twitchu altermmo_pl dopisali",
                                         "Za milion za bitcoina",
                                         "Za zdrowie wszystkich użytkowników tego discorda"])
                await asyncio.sleep(8)
                await ctx.channel.send('Uwaga!!! 3...')
                await asyncio.sleep(1)
                await ctx.channel.send('... 2...')
                await asyncio.sleep(1)
                await ctx.channel.send('... 1...')
                await asyncio.sleep(1)

                async with ctx.typing():
                    await ctx.channel.send('"**' + " ".join(cmd_req.upper()) + '**"')

                tries = 0
                try:
                    while True:
                        toast = await self.bot.wait_for('message', timeout=15)
                        response = str(toast.content)
                        if  response.lower() == cmd_req.lower():
                            cheerer = toast.author
                            await ctx.channel.send(f"Brawo {cheerer.name}!")

                            cheerer_id = str(cheerer.id)
                            try:
                                ranking[cheerer_id] += 1
                            except KeyError:
                                ranking[cheerer_id] = 1

                            break
                        else:
                            tries+=1
                except asyncio.TimeoutError:
                    async with ctx.typing():
                        await ctx.channel.send("Pfff... Miernoty. Nie wiecie jak się pije.")

            winner = max(ranking, key=ranking.get)
            image_name = "events/party/1.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            enter_desc = (f"Chopaki.. Hic!... Może jusz starczy?... Hic!... Graulasje <@{winner}>, jesteś nje do zdarsia... Hic!\n\n")

            ranking = sorted(ranking.items(), key=lambda x:x[1], reverse=True)
            ranking = dict(ranking)

            text = []
            x = 0
            for key, value in ranking.items():
                x += 1
                text.append(f"{x}. <@{key}>: {value}")

            exp_table = '\n'.join(text)

            description = enter_desc + exp_table
            #Embed create
            emb=discord.Embed(title='Ranking pijaków!', url='https://www.altermmo.pl/wp-content/uploads/zapierdol.gif', description=description, color=0xfcba03)
            emb.set_thumbnail(url='https://www.altermmo.pl/wp-content/uploads/zapierdol.gif')
            emb.set_footer(text='Jutro kac będzie okrutny...')
            await ctx.send(embed=emb)

            rarity = 2
            boost_percent = random.randint(0,50)
            guild = self.bot.get_guild(686137998177206281)
            user = guild.get_member(int(winner))

            await functions_daily.randLoot(self, ctx, rarity, user, boost_percent)

        else:
            image_name = "events/party/0.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            await ctx.send("No cóż... Pozostało napić się w samotności. " +
                           f"Zdrowie {cheerer.name}! <:Drink:912798939542061086>")

    global spawn_memory
    async def spawn_memory(self, ctx):
        """Function to spawn memory game."""

        e_title = f"<:PepoG:790963160528977980> Mędrzec potrzebuje pomocy! <:PepoG:790963160528977980>"

        e_descr = ('Witajcie, mam na imię Sanczo. Potrzebuję Waszej pomocy...'
                   '\n\nZa cholerę nie mogę spamiętać runicznych emotek, a brakuje mi rąk, żeby wertować kartki w księdze. Pomożecie mi?'
                   '\n\n*Zostaw reakcję pod postem, jeśli chcesz wziąć udział.*')

        e_color = 0x827b7a

        image_name = "events/memory/0.png"
        file=discord.File(image_name)
        await ctx.send(file=file)

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        msg = await ctx.send(embed=embed)

        #Define check function
        def check(reaction, user):
            return msg.channel == ctx.channel and str(reaction.emoji) == "<:PepoG:790963160528977980>" and user.id != 971322848616525874 and user.id != 859729615123251200 and msg.id == reaction.message.id

        await msg.add_reaction("<:PepoG:790963160528977980>")

        if DebugMode:
            timeout = 15
        else:
            timeout = 600

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=timeout, check=check)

            player = user
            msg = await ctx.send(f"*Doskonale <@{player.id}>!* - z entuzjazmem zareagował Sanczo. - *Teraz pokażę Ci 4 runiczne emotki, które zaraz będziesz musiał podać w odpowiedniej kolejności. Zapamiętaj je!*")

            emotes_list = ["<:MonkaChrist:783992337075929098>", "<:peepoBlush:984769061340737586>",
                           "<:Susge:973591024322633858>", "<:peepocof:1062666939178160188>",
                           "<:HYPERS:1080778935966642276>", "<:Pepega:936907616293093377>",
                           "<:Bedge:970576892874854400>", "<:EZ:720710033645633587>",
                           "<:gayge:1062110423438078072>", "<:POGGERS:936907543849078844>"]
            emotes_list_full = ["<:MonkaChrist:783992337075929098>",
                            "<:peepoBlush:984769061340737586>",
                           "<:Susge:973591024322633858>", "<:peepocof:1062666939178160188>",
                           "<:HYPERS:1080778935966642276>", "<:Pepega:936907616293093377>",
                           "<:Bedge:970576892874854400>", "<:EZ:720710033645633587>",
                           "<:gayge:1062110423438078072>", "<:POGGERS:936907543849078844>"]
            sel_emotes_list = []

            for x in range(0, 4):
                emote = random.choice(emotes_list)
                emotes_list.remove(emote)
                sel_emotes_list.append(emote)

                await msg.add_reaction(emote)

            await ctx.send("... 20...")
            await asyncio.sleep(10)
            await ctx.send("... 10...")
            await asyncio.sleep(5)
            await ctx.send("... 5...")
            await asyncio.sleep(5)

            await msg.delete()

            react_msg = await ctx.send(f"*Zaczynam przygotowywać zaklęcie. Zareaguj na tę wiadomości runicznymi emotkami w odpowiedniej kolejności <@{player.id}>!*")

            print(emotes_list_full)
            for emote in emotes_list_full:
                await react_msg.add_reaction(emote)

            #Define check function
            def check_emote(reaction, player):
                return user == player and react_msg.id == reaction.message.id

            index = 0
            for emote in sel_emotes_list:
                try:
                    print(react_msg.id)
                    reaction, user = await self.bot.wait_for('reaction_add', timeout=10, check=check_emote)
                    if str(sel_emotes_list[index]) == str(reaction.emoji):
                        await ctx.send("*Dobrze!* - Sanczo dopinguje.")
                        index += 1
                    else:
                        await ctx.send("*Całkiem nieźle idz...* - Sanczo nie zdążył dokończyć.")
                        image_name = "events/memory/1.png"
                        file=discord.File(image_name)
                        await ctx.send(file=file)
                        return False
                except asyncio.TimeoutError:
                    await ctx.send("*Ech, za wolno, poradzę sobie sam...* - rzekł Sanczo i zabrał się do pracy.")
                    await asyncio.sleep(5)
                    image_name = "events/memory/1.png"
                    file=discord.File(image_name)
                    await ctx.send(file=file)
                    return False

            rarity = random.randint(0,100)
            if rarity >= 50:
                rarity = 2
            else:
                rarity = 1
            boost_percent = random.randint(0,25)

            image_name = "events/memory/2.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            await ctx.send("*Doskonała robota! Zaklęcie gotowe!* - Twarz Sanczo promienieje z radości.")
            await functions_daily.randLoot(self, ctx, rarity, player, boost_percent)
            return True

        except asyncio.TimeoutError:
            await ctx.send("*Ech, poradzę sobie sam...* - rzekł Sanczo i zabrał się do pracy.")
            await asyncio.sleep(5)
            image_name = "events/memory/1.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            return False

    global spawn_hazard
    async def spawn_hazard(self, ctx):
        """Function to spawn hazard game."""

        e_title = f"<:tf:882182966606966794> Zaryzykuj! <:tf:882182966606966794>"

        e_descr = ('Witaj Wędrowcze, nazywam się Dova. Nie chcesz zgarnąć kilka zwojów?'
                   '\n\nWybierz jeden z dwóch kubków. Pod jednym z nich będzie się kryła nagroda.'
                   '\n\n*Zostaw reakcję pod postem, jeśli chcesz wziąć udział. Potrzebujesz 5 zwojów.*')

        e_color = 0x827b7a

        image_name = "events/hazard/0.jpeg"
        file=discord.File(image_name)
        await ctx.send(file=file)

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        msg = await ctx.send(embed=embed)

        print("Before")
        await msg.add_reaction("<:2Head:882184634572627978>")
        print("1")
        await msg.add_reaction("<:Nerdge:984770661702578227>")
        print("After")

        if DebugMode:
            timeout = 15
        else:
            timeout = 600

        emotes_list = ["<:2Head:882184634572627978>",
                        "<:Nerdge:984770661702578227>"]

        emote = random.choice(emotes_list)

        #Define check function
        def check_emote(reaction, user):
            print(user)
            return msg.id == reaction.message.id and user.id != 971322848616525874 and user.id != 859729615123251200

        try:
            while 1:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=timeout, check=check_emote)

                # Check if user has enough scrolls
                print("Checking if user has a pet...")
                sql = f"SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = {user.id};"
                for retries in range(0,3):
                    try:
                        scrolls = await self.bot.pg_con.fetch(sql)
                        break
                    except:
                        await ctx.channel.send(f"Błąd bazy danych <:Sadge:936907659142111273>... Próbuję ponownie - {retries}")
                else:
                    return False
                
                if scrolls:
                    reroll_scroll = scrolls[0][0]
                    if reroll_scroll >= 5:
            
                        if str(emote) == str(reaction.emoji):
                            await ctx.send("*Ech, gratuluje* - Dova rzekł zawiedziony.")
                            sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {reroll_scroll+5}
                            WHERE PLAYER_ID = {user.id};"""
                            await self.bot.pg_con.fetch(sql)
                            break
                        else:
                            sql = f"""UPDATE PETOWNER SET REROLL_SCROLL = {reroll_scroll-5}
                            WHERE PLAYER_ID = {user.id};"""
                            await self.bot.pg_con.fetch(sql)
                            await ctx.send("*Dzięki za zwoje. Kolego* <:2Head:882184634572627978> - Chwilę potem Dova znikł za drzwiami karczmy.")
                            return False
                    else:
                        await ctx.send("Najpierw musisz mieć czym grać. Potrzebujesz co najmniej 5 zwojów...")
                else:
                    await ctx.send("Najpierw musisz mieć czym grać. Potrzebujesz co najmniej 5 zwojów...")

        except asyncio.TimeoutError:
            await ctx.send("*Ech, na tym Discordzie nie ma pracy dla oszustów z moimi talentami...* - rzekł Dova i zniknął w cieniu.")
            return False

        rarity = 2
        boost_percent = random.randint(20,55)

        await functions_daily.randLoot(self, ctx, rarity, user, boost_percent)
        return True


    global spawn_hunting
    async def spawn_hunting(self, ctx):
        """Spawn a hunt object.
        """
        e_title = f"<:MonkaS:882181709100097587> Mroczny las! <a:PeepoRiot:1067317732942565416>"

        e_descr = ('Przed Wami złowrogi las... Czy odważycie się do niego wkroczyć?'
                '\n\n*Zostaw reakcję pod postem, jeśli chcesz wziąć udział.*')

        e_color = 0x003616

        image_name = "events/hunting/0.png"
        file=discord.File(image_name)
        await ctx.send(file=file)

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        msg = await ctx.send(embed=embed)

        #Define check function
        def check(reaction, user):
            return msg.channel == ctx.channel and str(reaction.emoji) == "⚔️" and user.id != 971322848616525874 and user.id != 859729615123251200 and msg.id == reaction.message.id

        await msg.add_reaction("⚔️")

        if DebugMode:
            timeout = 15
        else:
            timeout = 600

        try:
            reaction, user = await self.bot.wait_for('reaction_add', timeout=timeout, check=check)

            player = user

            rarity = random.randint(0,100)
            if 0 <= rarity <= 96:
                rarity = 1
            else:
                rarity = 2
            is_player_boss, boss_player = await functions_daily.fBossImage(self, ctx, rarity)
            await functions_daily.hunt_mobs(self, ctx, rarity, is_player_boss, boss_player, player)

        except asyncio.TimeoutError:
            await ctx.send("*Las pozostaje tajemnicą...*")
            image_name = "events/hunting/1.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            return False
        
    global spawn_duel
    async def spawn_duel(self, ctx):
        """Spawn a duel object.
        """
        e_title = f"<:MonkaS:882181709100097587> Pojedynek! <a:PeepoRiot:1067317732942565416>"

        e_descr = ('Przed Wami rozpościera się arena. Czy odważycie się zawalczyć na śmierć i życie?'
                '\n\n*Zostaw reakcję pod postem, jeśli chcesz wziąć udział.*')

        e_color = 0x360006

        image_name = "events/pvp/pvp.png"
        file=discord.File(image_name)
        await ctx.send(file=file)

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        msg = await ctx.send(embed=embed)

        participants = []

        #Define check function
        def check(reaction, user):
            return msg.channel == ctx.channel and str(reaction.emoji) == "⚔️" and user.id != 971322848616525874 and user.id != 859729615123251200 and msg.id == reaction.message.id and user not in participants

        await msg.add_reaction("⚔️")

        if DebugMode:
            timeout = 15
        else:
            timeout = 600

        while 1:
            try:
                reaction, user = await self.bot.wait_for('reaction_add', timeout=timeout, check=check)

                participants.append(user)
               
                if len(participants) >= 2:
                    break
                else:
                    if DebugMode:
                        await ctx.send(user.name + " wyzywa wszystkich na pojedynek! Jeśli nikt się nie zgłosi, to " + user.name + " otrzyma skarb oraz 6 punktów rankingowych!")
                    else:
                        await ctx.send(user.name + " wyzywa <@&985071779787730944> na pojedynek! Jeśli nikt się nie zgłosi, to " + user.name + " otrzyma skarb oraz 6 punktów rankingowych!")

            except asyncio.TimeoutError:
                if len(participants) > 0:
                    await functions_database.updateRankingTable(self, ctx,
                        participants[0].id, 2, 0)
                    await functions_daily.randLoot(self, ctx, 2, participants[0], 0)

                await ctx.send("*Arena pozostaje pusta...*")
                image_name = "events/pvp/0.png"
                file=discord.File(image_name)
                await ctx.send(file=file)
                return False

        global BUSY
        BUSY = 1
        success = await functions_boss.duelFight(self, ctx, participants)
        return success

    global assasinate
    async def assasinate(self, ctx):
        sql = f"SELECT REROLL_SCROLL FROM PETOWNER WHERE PLAYER_ID = {ctx.author.id};"

        for retries in range(0, 3):
            try:
                pet_exists = await self.bot.pg_con.fetch(sql)
                break
            except Exception as e:
                print(e)
                await ctx.channel.send(
                    f"Błąd bazy danych <:Sadge:936907659142111273>... Próbuję ponownie - {retries}"
                )
        else:
            return False

        if not pet_exists:
            await ctx.channel.send("Nie masz jeszcze peta/konta w systemie.")
            return False

        reroll_scroll = pet_exists[0][0]

        if reroll_scroll < 30:
            await ctx.channel.send("Potrzebujesz minimum 30 zwojów odrodzenia.")
            return False

        def check_assassination(author):
            def inner_check(message):
                return (
                    message.author == author
                    and message.channel == ctx.channel
                    and message.content.lower() == "$potwierdzam"
                )
            return inner_check

        await ctx.channel.send(
            f"Czy jesteś pewien, że chcesz zatrudnić zabójcę za 30 zwojów odrodzenia, "
            f"<@{ctx.author.id}>? <:Hmm:984767035617730620> Wpisz **$potwierdzam**."
        )

        try:
            confirm_cmd = await self.bot.wait_for(
                "message",
                timeout=15,
                check=check_assassination(ctx.author)
            )

            await confirm_cmd.add_reaction("<:RIP:912797982917816341>")

        except asyncio.TimeoutError:
            await ctx.channel.send("Nie potwierdzono zlecenia zabójstwa.")
            return False

        # Zabieramy koszt
        await self.bot.pg_con.execute(
            f"""
            UPDATE PETOWNER
            SET REROLL_SCROLL = REROLL_SCROLL - 30
            WHERE PLAYER_ID = {ctx.author.id};
            """
        )

        # 20% szans, że zabójca zdradzi zleceniodawcę
        if random.random() < 0.20:
            await ctx.channel.send(
                "Zabójca Cię zdradził! Wbija Ci nóż w plecy i zabiera zwoje... "
                "<:RIP:912797982917816341>"
            )
            await functions_boss.setDeadAssassin(self, ctx, ctx.author.id)
            return True

        dbRankingRead = await self.bot.pg_con.fetch(
            """
            SELECT ID
            FROM RANKING
            WHERE ID != $1
            ORDER BY POINTS DESC
            LIMIT 100;
            """,
            str(ctx.author.id)
        )

        if not dbRankingRead:
            await ctx.channel.send("Zabójca nie znalazł żadnego celu.")
            return False

        random_player = random.choice(dbRankingRead)
        target_id = random_player["id"] if "id" in random_player else random_player[0]

        await functions_boss.setDeadAssassin(self, ctx, target_id)

        await ctx.channel.send(
            f"Zabójca wykonał zlecenie. Ofiarą został <@{target_id}> "
            "<:RIP:912797982917816341>"
        )

        return True
    
    global spawn_pvp_tournament
    async def spawn_pvp_tournament(self, ctx):
        """Collects players for 3 minutes and runs a PvP bracket tournament."""

        if not DebugMode:
            await ctx.send(f"<@&985071779787730944>")

        participants = []
        reaction_emoji = "⚔️"

        e_title = f"<:MonkaS:882181709100097587> Tygodniowy turniej PvP! <a:PeepoRiot:1067317732942565416>"

        e_descr = ('Przed Wami rozpościera się arena. Czy odważycie się zawalczyć na śmierć i życie?'
                '\n\n*Zostaw reakcję pod postem, jeśli chcesz wziąć udział w turnieju.*')

        e_color = 0x360006

        image_name = "events/tournament/0.png"
        file=discord.File(image_name)
        await ctx.send(file=file)

        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        msg = await ctx.send(embed=embed)

        await msg.add_reaction(reaction_emoji)

        def check(reaction, user):
            return (
                reaction.message.id == msg.id
                and str(reaction.emoji) == reaction_emoji
                and not user.bot
            )

        if DebugMode:
            end_time = asyncio.get_event_loop().time() + 10
        else:
            end_time = asyncio.get_event_loop().time() + 180

        while asyncio.get_event_loop().time() < end_time:
            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add",
                    timeout=end_time - asyncio.get_event_loop().time(),
                    check=check
                )

                if user not in participants:
                    participants.append(user)
                    await ctx.send(f"{user.mention} dołączył do turnieju PvP!")

            except asyncio.TimeoutError:
                break

        if DebugMode:
            participants.append(participants[0])

        if len(participants) == 1:
            # await functions_database.updateRankingTable(self, ctx,
            #             participants[0].id, 4, 18)
            await functions_daily.randLoot(self, ctx, 2, participants[0], 0)
        elif len(participants) < 2 and not DebugMode:
            await ctx.send("PvP turniej anulowany. Za mało uczestników.")
            return None

        random.shuffle(participants)

        await ctx.send(
            "**Rejestracja na turniej zakończona!**\n"
            f"Uczestnicy: {', '.join(user.mention for user in participants)}"
        )

        round_number = 1

        while len(participants) > 1:
            await ctx.send(f"**Turniej PvP - Runda {round_number}**")

            next_round = []

            if len(participants) % 2 == 1:
                lucky_player = participants.pop()
                next_round.append(lucky_player)
                await ctx.send(f"{lucky_player.mention} szczęśliwie przechodzi do kolejnej rundy!")

            for i in range(0, len(participants), 2):
                player_1 = participants[i]
                player_2 = participants[i + 1]

                # await ctx.send(
                #     f"**Walka:** {player_1.mention} vs {player_2.mention}"
                # )

                finish, winner = await functions_boss.duelFight(self, ctx, [player_1, player_2], False)

                if winner is None:
                    winner = random.choice([player_1, player_2])
                    await ctx.send(
                        f"Remis. Losowy zwycięzca: {winner.mention}."
                    )

                next_round.append(winner)

                await ctx.send(f"**Zwycięzca:** {winner.mention}")

            participants = next_round
            round_number += 1

        tournament_winner = participants[0]

        image_name = "events/tournament/1.gif"
        file=discord.File(image_name)
        await ctx.send(file=file)

        await ctx.send(
            f"🏆 **Zwycięzca turnieju:** {tournament_winner.mention}! Gratulacje! 🏆"
        )
        # await functions_database.updateRankingTable(self, ctx,
        #                 tournament_winner.id, 4, 18)

        return tournament_winner

    global spawn_ritual
    async def spawn_ritual(self, ctx):
        """Function to spawn ritual."""

        print("Ritual spawn.")

        e_title = "<:MonkaChrist:783992337075929098> Rozpoczął się rytuał przywołania! <:MonkaS:882181709100097587>"

        e_descr = ('Użyczcie swojej energii w rytuale przywołania!\n\n'
        '**Zaoferujcie swoją pomoc zostawiając reakcję pod tym postem.** Im więcej Was będzie,'
        'tym potężniejszą istotę przywołacie!')

        e_color = 0xFC0303

        image_name = "events/ritual/" + str(random.randint(0,3)) + ".png"
        file=discord.File(image_name)

        #image
        embed = discord.Embed(
            title=e_title,
            description=e_descr,
            color=e_color)

        await ctx.channel.send(file=file)
        msg = await ctx.send(embed=embed)

        await msg.add_reaction("✨")
        if DebugMode:
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(600)

        users = []

        try:
            message = await ctx.channel.fetch_message(msg.id)
        except discord.NotFound:
            print("Ritual interrupted.")
            return False, 0, False, "Alter"

        for reaction in message.reactions:
            async for user in reaction.users():
                guild = message.guild
                if guild.get_member(user.id) is not None and user.id != 859729615123251200 and user.id != 971322848616525874 and user not in users:
                    users.append(user)

        n = len(users)

        def roll_based_on_list(lst):
            n = len(lst)

            max_n = 12          
            mu = min(n / max_n, 1) * 4

            sigma = 1.0

            value = round(random.gauss(mu, sigma))

            return max(0, min(4, value))

        if n > 0:
            global BUSY
            global BOSSRARITY
            global EVENT_ALIVE

            BOSSRARITY = roll_based_on_list(users)

            # Load all modifiers from file
            modifiers = await functions_modifiers.load_modifiers(self, ctx)
            BOSSRARITY += modifiers["rarity_boost"]
            if BOSSRARITY == 3 or BOSSRARITY > 4:
                BOSSRARITY = 4

            EVENT_ALIVE = 0
            BUSY = 0
            await functions_general.fClear(self, ctx)

            #Send boss image based on rarity
            global initCommand, is_player_boss, player_boss
            initCommand = "zaatakuj"
            is_player_boss, player_boss = await functions_boss.fBossImage(self, ctx, BOSSRARITY)
            return True, BOSSRARITY, is_player_boss, player_boss
        else:
            image_name = "events/ritual/4.png"
            file=discord.File(image_name)
            await ctx.send(file=file)
            await ctx.send('Rytuał przywołania nie powiódł się... <:Sadge:936907659142111273>')
            return False, 0, False, "Alter"


def setup(bot):
    """Load the FunctionsEvents cog."""
    bot.add_cog(FunctionsEvents(bot))
