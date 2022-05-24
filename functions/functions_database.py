from discord.ext import commands
import discord
import asyncio

import datetime
import os
import urllib.parse as urlparse
import psycopg2

# database bag for functions

class functions_database(commands.Cog, name="functions_database"):
    def __init__(self, bot):
        self.bot = bot

    global connectToDB
    def connectToDB():
        try:
            #Establishing the connection
            #dbConnection = psycopg2.connect("dbname='d2am99h8cekkeo' user='hkheidzoebbhxe' password='d545d32cdd85d3018184ff7f82a9129180f577ed9e03e71f9fe05e93d9cd19ee' host='ec2-63-35-156-160.eu-west-1.compute.amazonaws.com' sslmode='require'")
            DATABASE_URL = os.environ['DATABASE_URL']
            dbConnection = psycopg2.connect(DATABASE_URL, sslmode='require')
            print ("Connected to the database.")
            #Creating a cursor object using the cursor() method
        except:
            print ("I am unable to connect to the database.")
        dbCursor = dbConnection.cursor()
        print ("Cursor created.")

        return dbConnection, dbCursor

    # ==================================== COMMANDS FOR BOSS DATABASE =======================================================================

    global createBossTable
    def createBossTable(dbCursor):
        #Doping EMPLOYEE table if already exists.
        dbCursor.execute("DROP TABLE IF EXISTS BOSS")
        #Creating table as per requirement
        sql ='''CREATE TABLE BOSS(
           ID INT,
           RARITY INT,
           RESPAWN_TIME TIMESTAMP,
           RESPAWN_STARTED BOOLEAN
        )'''
        dbCursor.execute(sql)
        print("Table created successfully.")
        dt = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        dbCursor.execute('INSERT INTO BOSS (ID, RARITY, RESPAWN_TIME, RESPAWN_STARTED) VALUES (%s,%s,%s,%s)', (0, 1 ,dt, True))
        print("Data inserted into Database.")

    global updateBossTable
    def updateBossTable(dbCursor, BossRarity, respawnTime, ResumeSpawn):
        #Database Update
        print("Conversion...")
        intRespawnTime = int(respawnTime)
        print("To datetime...")
        Time = datetime.datetime.utcnow() + datetime.timedelta(hours=2) + datetime.timedelta(seconds=intRespawnTime)
        print("Time before database write: " + str(Time))
        print("Save resume before database write: " + str(ResumeSpawn))
        print("Boss rarity before database write: " + str(BossRarity))
        print("Trying to update Database...")
        dbCursor.execute('UPDATE BOSS SET ID = (%s), RARITY = (%s), RESPAWN_TIME = (%s), RESPAWN_STARTED= (%s) WHERE ID = 0', (0, BossRarity , Time, ResumeSpawn))
        print("Data updated in Database.")


    global readBossTable
    def readBossTable(dbCursor):
        #Database Reading
        dbCursor.execute('SELECT RARITY, RESPAWN_TIME, RESPAWN_STARTED FROM BOSS LIMIT 1')
        dbBossRead = dbCursor.fetchall()
        #spawnTimestamp = datetime.datetime.strptime(str(dbBossRead[0][2]).rstrip(), "%Y-%m-%d %H:%M:%S.%f")
        print("Resume read from database: " + str(dbBossRead[0][2]))
        return dbBossRead[0][0], dbBossRead[0][1], dbBossRead[0][2]

    global closeTable
    def closeTable(dbConnection):
        dbConnection.close()
        print("Connection with a database closed.")

def setup(bot):
    bot.add_cog(functions_database(bot))
