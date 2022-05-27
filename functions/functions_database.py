from discord.ext import commands
import discord
import asyncio

import datetime
import os
import urllib.parse as urlparse
import psycopg2
import asyncpg
from asyncpg.pool import create_pool


# database bag for functions
class functions_database(commands.Cog, name="functions_database"):
    def __init__(self, bot):
        self.bot = bot

    global create_db_pool
    async def create_db_pool(self):
        try:
            #Establishing the connection
            self.bot.pg_con = await asyncpg.create_pool(database="d2am99h8cekkeo", user="hkheidzoebbhxe", password='d545d32cdd85d3018184ff7f82a9129180f577ed9e03e71f9fe05e93d9cd19ee', host='ec2-63-35-156-160.eu-west-1.compute.amazonaws.com')
            print("Connected to database. Pool created.")
            #Creating a cursor object using the cursor() method
        except:
            print ("I am unable to connect to the database.")
        #dbCursor = dbConnection.cursor()
        #print ("Cursor created.")

    # ==================================== COMMANDS FOR BOSS DATABASE =======================================================================

    #===== Create Tables

    global createBossTable
    async def createBossTable(self):
        #Doping EMPLOYEE table if already exists.
        await self.bot.pg_con.execute("DROP TABLE IF EXISTS BOSS")
        #Creating table as per requirement
        sql ='''CREATE TABLE BOSS(
           ID NUMERIC,
           RARITY NUMERIC,
           RESPAWN_TIME TIMESTAMP,
           RESPAWN_STARTED BOOLEAN
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table created successfully.")
        dt = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        d = dt.replace(microsecond=0)
        print('INSERT INTO BOSS (ID, RARITY, RESPAWN_TIME, RESPAWN_STARTED) VALUES ({},{},\'{}\',{});'.format(str(0), str(1) , str(d), str(True)))
        await self.bot.pg_con.execute('INSERT INTO BOSS (ID, RARITY, RESPAWN_TIME, RESPAWN_STARTED) VALUES ({},{},\'{}\',{});'.format(str(0), str(1) , str(d), str(True)))
        print("Data inserted into Database.")

    global createRecordTable
    async def createRecordTable(self):
        #Doping RECORD table if already exists.
        await self.bot.pg_con.execute("DROP TABLE IF EXISTS RECORD")
        #Creating table as per requirement
        sql ='''CREATE TABLE RECORD(
           ID NUMERIC,
           RECORD_TIME VARCHAR(255),
           NICK VARCHAR(255)
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table RECORD created successfully.")
        defaultRecord = '00:20.000'
        defaultNick = 'Nieznajomy'
        print('INSERT INTO RECORD (ID, RECORD_TIME, NICK) VALUES ({},\'{}\',\'{}\');'.format(str(0), defaultRecord, defaultNick))
        await self.bot.pg_con.execute('INSERT INTO RECORD (ID, RECORD_TIME, NICK) VALUES ({},\'{}\',\'{}\');'.format(str(0), defaultRecord, defaultNick))
        print("Data inserted into Record Database.")


    global createHistoryTable
    async def createHistoryTable(self):
        #Doping History table if already exists.
        await self.bot.pg_con.execute("DROP TABLE IF EXISTS HISTORY")
        #Creating table as per requirement
        sql ='''CREATE TABLE HISTORY(
           ID NUMERIC,
           FIGHT_TIME VARCHAR(255),
           NICK VARCHAR(255)
        )'''
        await self.bot.pg_con.execute(sql)
        print("Table HISTORY created successfully.")
        defaultTime = '2020-05-27 15:00:00'
        defaultNick = 'Nieznajomy'
        print('INSERT INTO HISTORY (ID, FIGHT_TIME, NICK) VALUES ({},\'{}\',\'{}\');'.format(str(0), defaultTime, defaultNick))
        await self.bot.pg_con.execute('INSERT INTO HISTORY (ID, FIGHT_TIME, NICK) VALUES ({},\'{}\',\'{}\');'.format(str(0), defaultTime, defaultNick))
        print("Data inserted into History Database.")

    # ====== Update Tables

    global updateBossTable
    async def updateBossTable(self, ctx, BossRarity, respawnTime, ResumeSpawn):
        #Database Update
        print("Conversion...")
        intRespawnTime = int(respawnTime)
        print("To datetime...")
        Time = datetime.datetime.utcnow() + datetime.timedelta(hours=2) + datetime.timedelta(seconds=intRespawnTime)
        d = Time.replace(microsecond=0)
        print("Time before database write: " + str(type(d)))
        print("Save resume before database write: " + str(type(ResumeSpawn)))
        print("Boss rarity before database write: " + str(type(BossRarity)))
        print("Trying to update Database...")
        print("UPDATE BOSS SET ID = {}, RARITY = {}, RESPAWN_TIME = \'{}\', RESPAWN_STARTED = {} WHERE ID = 0".format(str(0), str(BossRarity), str(d), str(ResumeSpawn)))
        await self.bot.pg_con.execute('UPDATE BOSS SET ID = {}, RARITY = {}, RESPAWN_TIME = \'{}\', RESPAWN_STARTED = {} WHERE ID = 0'.format(str(0), str(BossRarity) , str(d), str(ResumeSpawn)))
        print("Data updated in Database.")

    global updateRecordTable
    async def updateRecordTable(self, ctx, Nick, recordTime_MM_SS_MS):
        #Database Update
        #test conversion
        stringRecord = str(recordTime_MM_SS_MS).lstrip(' ')
        print("Saved record: "+ stringRecord)
        print("Time before database write: " + stringRecord)
        print("Trying to update Database...")
        print("UPDATE RECORD SET ID = {}, RECORD_TIME = \'{}\', NICK = \'{}\' WHERE ID = 0".format(str(0), str(recordTime_MM_SS_MS), str(Nick)))
        await self.bot.pg_con.execute("UPDATE RECORD SET ID = {}, RECORD_TIME = \'{}\', NICK = \'{}\' WHERE ID = 0".format(str(0), str(recordTime_MM_SS_MS), str(Nick)))
        #print("Data updated in Record Database.")

    global updateHistoryTable
    async def updateHistoryTable(self, ctx, Nick, fightTime):
        #Database Update
        stringRecord = str(fightTime)
        print("Time before database write: " + stringRecord)
        print("Trying to update Database...")
        print("UPDATE HISTORY SET ID = {}, FIGHT_TIME = \'{}\', NICK = \'{}\' WHERE ID = 0".format(str(0), str(fightTime), str(Nick)))
        await self.bot.pg_con.execute("UPDATE HISTORY SET ID = {}, FIGHT_TIME = \'{}\', NICK = \'{}\' WHERE ID = 0".format(str(0), str(fightTime), str(Nick)))
        #print("Data updated in Record Database.")

    # ====== Read Tables

    global readBossTable
    async def readBossTable(self, ctx):
        #Database Reading
        dbBossRead = await self.bot.pg_con.fetch("SELECT RARITY, RESPAWN_TIME, RESPAWN_STARTED FROM boss")
        #spawnTimestamp = datetime.datetime.strptime(str(dbBossRead[0][2]).rstrip(), "%Y-%m-%d %H:%M:%S")
        print("Rarity read from database: " + str(dbBossRead[0][0]))
        print("Respawn time read from database: " + str(dbBossRead[0][1]))
        print("Resume read from database: " + str(dbBossRead[0][2]))
        return dbBossRead[0][0], dbBossRead[0][1], dbBossRead[0][2]

    global readRecordTable
    async def readRecordTable(self, ctx):
        #Database Reading
        dbBossRead = await self.bot.pg_con.fetch("SELECT RECORD_TIME, NICK FROM RECORD")
        print("Nick read from database: " + str(dbBossRead[0][1]))
        print("Record time read from database: " + str(dbBossRead[0][0]))
        return dbBossRead[0][0], dbBossRead[0][1]

    global readHistoryTable
    async def readHistoryTable(self, ctx):
        #Database Reading
        dbBossRead = await self.bot.pg_con.fetch("SELECT FIGHT_TIME, NICK FROM HISTORY")
        print("Nick read from database: " + str(dbBossRead[0][1]))
        print("Fight time read from database: " + str(dbBossRead[0][0]))
        return dbBossRead[0][0], dbBossRead[0][1]

def setup(bot):
    bot.add_cog(functions_database(bot))
