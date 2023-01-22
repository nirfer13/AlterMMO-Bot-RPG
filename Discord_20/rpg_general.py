import discord
from discord.errors import ClientException
from discord.ext import commands, tasks

import functions_rpg_general

#Import Globals
from globals.globalvariables import DebugMode

class message(commands.Cog, name="rpg_general"):
    def __init__(self, bot):
        self.bot = bot

    #========================== COMMANDS FOR USERS =======================================

    @commands.command(name="start")
    @commands.has_permissions(administrator=True) #TOREMOVE
    async def createCharacter(self, ctx):
        playerID = ctx.author.id
        print("User ID: " + str(playerID))
        await functions_rpg_general.createCharacter(self, ctx, playerID)

    @commands.command(name="rankinglvl")
    @commands.has_permissions(administrator=True) #TOREMOVE
    async def readRpgGeneral(self, ctx):
        await functions_rpg_general.readRpgGeneral(self, ctx)

    @commands.command(name="profil")
    @commands.has_permissions(administrator=True) #TOREMOVE
    async def checkGeneralProfile(self, ctx):
        await functions_rpg_general.checkGeneralProfile(self, ctx)

    @commands.command(name="odpoczynek")
    @commands.has_permissions(administrator=True) #TOREMOVE
    async def regenHPMP(self, ctx):
        await functions_rpg_general.regenHPMP(self, ctx)


    #========================== COMMANDS TO DEBUG =======================================

    @commands.command(name="createRpgGeneralDatabase")
    @commands.has_permissions(administrator=True)
    async def createRpgGeneralDatabase(self, ctx):
        await ctx.channel.send("Baza danych RPG tworzona...")
        await functions_rpg_general.createRpgGeneralTable(self, ctx)
        await ctx.channel.send("Baza danych RPG utworzona.")

    @commands.command(name="createHeroStatsDatabase")
    @commands.has_permissions(administrator=True)
    async def createHeroStatsTable(self, ctx):
        await ctx.channel.send("Baza danych statystyk bohaterow tworzona...")
        await functions_rpg_general.createHeroStatsTable(self, ctx)
        await ctx.channel.send("Baza danych statystyk bohaterow utworzona.")

    @commands.command(name="calcStats")
    @commands.has_permissions(administrator=True)
    async def calcStats(self, ctx, ID, new: bool):
        await functions_rpg_general.calcStats(self,ctx,ID, new)

    @commands.command(name="setHPMP")
    @commands.has_permissions(administrator=True)
    async def setHPMP(self, ctx, ID, HP, MP):
        await functions_rpg_general.updateHPMPHeroStats(self,ctx, ID, HP, MP)

    @commands.command(name="addExp")
    @commands.has_permissions(administrator=True)
    async def addExp(self, ctx, ID, EXP):
        await functions_rpg_general.updateExpHeroStats(self,ctx, ID, EXP)

    @commands.command(name="readStats")
    @commands.has_permissions(administrator=True)
    async def readStats(self, ctx, ID):
        await functions_rpg_general.readHeroStatsTable(self, ctx, ID)

    @commands.command(name="setStats")
    @commands.has_permissions(administrator=True)
    async def setStats(self, ctx, ID, Str, Agi, Int, Stm, Rempoints):
        await functions_rpg_general.updateStatsRPGGeneral(self, ctx, ID, Str, Agi, Int, Stm, Rempoints)

    @commands.command(name="spawnMob")
    @commands.has_permissions(administrator=True)
    async def spawnMob(self, ctx, mobLvl: int):
        await functions_rpg_general.spawnMob(self, ctx, mobLvl)

    @commands.command(name="spawnPlayer")
    @commands.has_permissions(administrator=True)
    async def spawnPlayer(self, ctx, playerID):
        await functions_rpg_general.spawnPlayer(self, ctx, playerID)
        
def setup(bot):
    bot.add_cog(message(bot))
