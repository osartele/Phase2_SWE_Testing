import random

input = int(input("Please give a number between 10 and 20: "))

arr = [0] * input

print("The state of the array before initialization:", arr)

for i in range(len(arr)):
    arr[i] = random.randint(0, 200)

print("The state of the array after initialization:", arr)
print("The odd numbers are: ", {x for x in arr if x % 2 == 1})
print("The even numbers are: ", {x for x in arr if x % 2 == 0})
print("The maximum is: ", max(arr))
print("The minimum is: ", min(arr))
print("The sum is: ", sum(arr))
print("The average is: ", sum(arr) / len(arr))
