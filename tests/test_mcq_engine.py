import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import unittest
from ai_engine.mcq_engine import parse_mcq_test, parse_answer_key_section

SAMPLE_PDF_TEXT = """
Operating Systems — 10 MCQ Test
Instructions: Choose the best answer for each question. Try the test without looking at the answer key.

1. What is the primary function of an Operating System?
A. To design websites
B. To manage computer hardware and software resources
C. To create databases
D. To compile only Java programs

2. Which of the following is NOT a function of an Operating System?
A. Memory management
B. Process management
C. File management
D. Manufacturing CPU hardware

3. Which scheduling algorithm gives the CPU to the process with the smallest execution time
first?
A. FCFS
B. Round Robin
C. Shortest Job First
D. Priority Scheduling

4. Which data structure is commonly used to implement a process queue?
A. Queue
B. Stack
C. Tree
D. Graph

5. A process that is waiting for an I/O operation to complete is in which state?
A. Running
B. Ready
C. Waiting/Blocked
D. Terminated

6. Which technique allows multiple processes to share the CPU by giving each a fixed time
slice?
A. Round Robin 
B. FCFS
C. SJF
D. FIFO Page Replacement

7. Which of the following is a necessary condition for deadlock?
A. Mutual exclusion
B. Compilation
C. Paging
D. Spooling

8. Which memory management technique divides physical memory into fixed-size blocks?
A. Segmentation
B. Paging
C. Compaction
D. Swapping

9. Which page replacement algorithm replaces the page that has not been used for the longest
time?
A. FIFO
B. LRU
C. Optimal
D. Round Robin

10. Which system call is commonly associated with creating a new process in Unix/Linux?
A. fork()
B. printf()
C. malloc()
D. scanf()

Answer Key
1. B
2. D
3. C
4. A
5. C
6. A
7. A
8. B
9. B
10. A
"""

class TestMCQEngine(unittest.TestCase):
    def test_parse_sample_pdf(self):
        answers = parse_answer_key_section(SAMPLE_PDF_TEXT)
        self.assertEqual(len(answers), 10)
        self.assertEqual(answers[1], "B")
        self.assertEqual(answers[2], "D")
        self.assertEqual(answers[3], "C")
        self.assertEqual(answers[4], "A")
        self.assertEqual(answers[5], "C")
        self.assertEqual(answers[6], "A")
        self.assertEqual(answers[7], "A")
        self.assertEqual(answers[8], "B")
        self.assertEqual(answers[9], "B")
        self.assertEqual(answers[10], "A")

        questions = parse_mcq_test(SAMPLE_PDF_TEXT)
        self.assertEqual(len(questions), 10)

        # Check Q1
        self.assertIn("primary function of an Operating System", questions[0]["question"])
        self.assertEqual(len(questions[0]["options"]), 4)
        self.assertEqual(questions[0]["answer"], "To manage computer hardware and software resources")

        # Check Q2
        self.assertEqual(questions[1]["answer"], "Manufacturing CPU hardware")

        # Check Q3
        self.assertEqual(questions[2]["answer"], "Shortest Job First")

        # Check Q6 (split across lines)
        self.assertEqual(questions[5]["answer"], "Round Robin")

        # Check Q10
        self.assertEqual(questions[9]["answer"], "fork()")
        print("All MCQ test assertions passed successfully!")

if __name__ == "__main__":
    unittest.main()
